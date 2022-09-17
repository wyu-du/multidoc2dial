import logging
import os
from typing import List, Tuple

import numpy as np
import psutil
import torch
import torch.distributed as dist

from dialdoc.models.rag.retrieval_rag_dialdoc import DialDocRagRetriever


logger = logging.getLogger(__name__)


class RagPyTorchDistributedRetriever(DialDocRagRetriever):
    """
    A distributed retriever built on top of the ``torch.distributed`` communication package. During training all workers
    initialize their own instance of the retriever, however, only the main worker loads the index into memory. The index is stored
    in cpu memory. The index will also work well in a non-distributed setup.

    Args:
        config (:class:`~transformers.RagConfig`):
            The configuration of the RAG model this Retriever is used with. Contains parameters indicating which ``Index`` to build.
        question_encoder_tokenizer (:class:`~transformers.PreTrainedTokenizer`):
            The tokenizer that was used to tokenize the question.
            It is used to decode the question and then use the generator_tokenizer.
        generator_tokenizer (:class:`~transformers.PreTrainedTokenizer`):
            The tokenizer used for the generator part of the RagModel.
        index (:class:`~transformers.models.rag.retrieval_rag.Index`, optional, defaults to the one defined by the configuration):
            If specified, use this index instead of the one built using the configuration
    """

    def __init__(self, config, question_encoder_tokenizer, generator_tokenizer, index=None):
        super().__init__(
            config,
            question_encoder_tokenizer=question_encoder_tokenizer,
            generator_tokenizer=generator_tokenizer,
            index=index,
            init_retrieval=False,
        )
        self.process_group = None

    def init_retrieval(self, distributed_port: int):
        """
        Retriever initialization function, needs to be called from the training process. The function sets some common parameters
        and environment variables. On top of that, (only) the main process in the process group loads the index into memory.

        Args:
            distributed_port (:obj:`int`):
                The port on which the main communication of the training run is carried out. We set the port for retrieval-related
                communication as ``distributed_port + 1``.
        """

        logger.info("initializing retrieval")

        # initializing a separate process group for retrieval as the default
        # nccl backend doesn't support gather/scatter operations while gloo
        # is too slow to replace nccl for the core gpu communication
        if dist.is_initialized():
            logger.info("dist initialized")
            # needs to be set manually
            os.environ["GLOO_SOCKET_IFNAME"] = self._infer_socket_ifname()
            # avoid clash with the NCCL port
            os.environ["MASTER_PORT"] = str(distributed_port + 1)
            self.process_group = dist.new_group(ranks=None, backend="gloo")

        # initialize retriever only on the main worker
        if not dist.is_initialized() or self._is_main():
            logger.info("dist not initialized / main")
            self.index.init_index()

        # all processes wait untill the retriever is initialized by the main process
        if dist.is_initialized():
            torch.distributed.barrier(group=self.process_group)

    def _is_main(self):
        return dist.get_rank(group=self.process_group) == 0

    def _scattered(self, scatter_list, target_shape, target_type=torch.float32):
        target_tensor = torch.empty(target_shape, dtype=target_type)
        dist.scatter(target_tensor, src=0, scatter_list=scatter_list, group=self.process_group)
        return target_tensor

    def _infer_socket_ifname(self):
        addrs = psutil.net_if_addrs()
        # a hacky way to deal with varying network interface names
        ifname = next((addr for addr in addrs if addr.startswith("e")), None)
        return ifname

    def retrieve(
        self,
        combined_hidden_states: np.ndarray,
        current_hidden_states: np.ndarray,
        history_hidden_states: np.ndarray,
        n_docs: int,
        dialog_lengths: List[Tuple] = None,
        domain: List[str] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[dict]]:
        """
        Retrieves documents for specified ``question_hidden_states``. The main process, which has the access to the index stored in memory, gathers queries
        from all the processes in the main training process group, performs the retrieval and scatters back the results.

        Args:
            question_hidden_states (:obj:`np.ndarray` of shape :obj:`(batch_size, vector_size)`):
                A batch of query vectors to retrieve with.
            n_docs (:obj:`int`):
                The number of docs retrieved per query.

        Output:
            retrieved_doc_embeds (:obj:`np.ndarray` of shape :obj:`(batch_size, n_docs, dim)`
                The retrieval embeddings of the retrieved docs per query.
            doc_ids (:obj:`np.ndarray` of shape :obj:`batch_size, n_docs`)
                The ids of the documents in the index
            doc_dicts (:obj:`List[dict]`):
                The retrieved_doc_embeds examples per query.
        """

        # single GPU training
        if not dist.is_initialized():
            doc_ids, retrieved_doc_embeds, doc_scores = self._main_retrieve(
                combined_hidden_states, current_hidden_states, history_hidden_states, n_docs, dialog_lengths, domain
            )
            return retrieved_doc_embeds, doc_ids, doc_scores, self.index.get_doc_dicts(doc_ids)

        # distributed training
        world_size = dist.get_world_size(group=self.process_group)

        # gather logic
        gather_list_1 = None
        gather_list_2 = None
        gather_list_3 = None
        if self._is_main():
            gather_list_1 = [torch.empty(combined_hidden_states.shape, dtype=torch.float32) for _ in range(world_size)]
            gather_list_2 = [torch.empty(current_hidden_states.shape, dtype=torch.float32) for _ in range(world_size)]
            gather_list_3 = [torch.empty(history_hidden_states.shape, dtype=torch.float32) for _ in range(world_size)]
        dist.gather(torch.tensor(combined_hidden_states), dst=0, gather_list=gather_list_1, group=self.process_group)
        dist.gather(torch.tensor(current_hidden_states), dst=0, gather_list=gather_list_2, group=self.process_group)
        dist.gather(torch.tensor(history_hidden_states), dst=0, gather_list=gather_list_3, group=self.process_group)

        # scatter logic
        n_queries = combined_hidden_states.shape[0]
        scatter_ids = []
        scatter_vectors = []
        scatter_scores = []
        if self._is_main():
            assert len(gather_list_1) == len(gather_list_2) == len(gather_list_3) == world_size
            comb_h_s = torch.cat(gather_list_1).numpy()
            curr_h_s = torch.cat(gather_list_2).numpy()
            hist_h_s = torch.cat(gather_list_3).numpy()
            ids, vectors, scores = self._main_retrieve(comb_h_s, curr_h_s, hist_h_s, n_docs, dialog_lengths, domain)
            ids, vectors, scores = torch.tensor(ids), torch.tensor(vectors), torch.tensor(scores)
            scatter_ids = self._chunk_tensor(ids, n_queries)
            scatter_vectors = self._chunk_tensor(vectors, n_queries)
            scatter_scores = self._chunk_tensor(scores, n_queries)

        doc_ids = self._scattered(scatter_ids, [n_queries, n_docs], target_type=torch.int64)
        retrieved_doc_embeds = self._scattered(scatter_vectors, [n_queries, n_docs, combined_hidden_states.shape[1]], torch.float32)
        doc_scores = self._scattered(scatter_scores, [n_queries, n_docs], torch.float32)

        return retrieved_doc_embeds.numpy(), doc_ids.numpy(), doc_scores.numpy(), self.index.get_doc_dicts(doc_ids)
