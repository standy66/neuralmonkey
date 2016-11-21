"""Running of a recurrent decoder.

This module aggragates what is necessary to run efficiently a recurrent
decoder. Unlike the default runner which assumes all outputs are independent on
each other, this one does not make any of these assumptions. It implements
model ensembling and beam search.

The TensorFlow session is invoked for every single output of the decoder
separately which allows ensembling from all sessions and do the beam pruning
before the a next output is emmited.
"""


from typing import List, NamedTuple, Tuple
import numpy as np
import tensorflow as tf

from neuralmonkey.tf_manager import RunResult
from neuralmonkey.runners.base_runner import BaseRunner, \
    Executable, ExecutionResult, NextExecute
from neuralmonkey.vocabulary import END_TOKEN_INDEX

# tests: mypy,pylint


# pylint: disable=invalid-name
BeamBatch = NamedTuple('BeamBatch',
                       [('decoded', np.ndarray),
                        ('logprobs', np.ndarray)])
ExpandedBeamBatch = NamedTuple('ExpandedBeamBatch',
                               [('beam_batch', BeamBatch),
                                ('next_logprobs', np.ndarray)])

# pylint: disable=too-many-locals


def _score_expanded(n: int,
                    batch_size: int,
                    expanded: List[ExpandedBeamBatch],
                    scoring_function) -> \
        Tuple[List[np.ndarray], List[np.ndarray]]:
    next_beam_hypotheses = []
    next_beam_logprobs = []
    # agregate the expanded hypotheses hypothsis-wise
    for rank in range(batch_size):
        candidate_scores = None
        candidate_hypotheses = None
        candidate_logprobs = None

        for expanded_batch in expanded:
            next_distribution = expanded_batch.next_logprobs[rank]
            if expanded_batch.beam_batch is None:
                expanded_hypotheses = np.expand_dims(np.arange(
                    len(next_distribution)), axis=1)
                expanded_logprobs = np.expand_dims(next_distribution, 1)
            else:
                hypothesis = expanded_batch.beam_batch.decoded[rank]
                prev_logprobs = expanded_batch.beam_batch.logprobs[rank]

                expanded_hypotheses = np.array(
                    [np.append(hypothesis, index)
                     for index, _ in enumerate(next_distribution)])
                expanded_logprobs = np.array(
                    [np.append(prev_logprobs, logprob)
                     for logprob in next_distribution])

            scores = scoring_function(expanded_hypotheses, expanded_logprobs)
            n_best_indices = np.argsort(scores)[-n:]
            candidate_scores = _try_append(
                candidate_scores, scores[n_best_indices])
            candidate_hypotheses = _try_append(
                candidate_hypotheses, expanded_hypotheses[n_best_indices])
            candidate_logprobs = _try_append(
                candidate_logprobs, expanded_logprobs[n_best_indices])

        n_best_indices = np.argsort(candidate_scores)[-n:]
        n_best_hypotheses = candidate_hypotheses[n_best_indices]
        n_best_logprobs = candidate_logprobs[n_best_indices]

        next_beam_hypotheses.append(n_best_hypotheses)
        next_beam_logprobs.append(n_best_logprobs)
    return next_beam_hypotheses, next_beam_logprobs


def _try_append(first, second):
    if first is None:
        return second
    else:
        return np.append(first, second)


def likelihood_beam_score(decoded, logprobs):
    """Score the beam by normalized probaility."""

    mask = []
    for hypothesis in decoded:
        before_end = True
        hyp_mask = []
        for index in hypothesis:
            hyp_mask.append(float(before_end))
            before_end &= (index != END_TOKEN_INDEX)
        mask.append(hyp_mask)

    mask_matrix = np.array(mask)
    masked_logprobs = mask_matrix * logprobs
    # pylint: disable=no-member
    avg_logprobs = masked_logprobs.sum(
        axis=1) - np.log(mask_matrix.sum(axis=1))
    return avg_logprobs


def n_best(n: int,
           expanded: List[ExpandedBeamBatch],
           scoring_function) -> List[BeamBatch]:
    """Take n-best from expanded beam search hypotheses.

    To do the scoring we need to "reshape" the hypohteses. Before the scoring
    the hypothesis are split into beam batches by their position in the beam.
    To do the scoring, however, they need to be organized by the instances.
    After the scoring, only _n_ hypotheses is kept for each isntance. These
    are again split by their position in the beam.

    Args:
        n: Beam size.
        expanded: List of batched expanded hypotheses.
        scoring_function: A function

    Returns:
        List of BeamBatches ready for new expansion.

    """

    batch_size = expanded[0].next_logprobs.shape[0]
    next_beam_hypotheses, next_beam_logprobs = _score_expanded(
        n, batch_size, expanded, scoring_function)

    # now cut the beams by hypotheses rank
    beam_batches = []
    for rank in range(n):
        hypotheses = np.array([beam[rank] for beam in next_beam_hypotheses])
        logprobs = np.array([beam[rank] for beam in next_beam_logprobs])
        beam_batches.append(BeamBatch(hypotheses, logprobs))

    return beam_batches


class RuntimeRnnRunner(BaseRunner):
    """Prepare running the RNN decoder step by step."""

    def __init__(self, output_series, decoder,
                 beam_size=1, beam_scoring_f=likelihood_beam_score):
        super(RuntimeRnnRunner, self).__init__(output_series, decoder)

        self._initial_fetches = [decoder.runtime_rnn_states[0]]
        self._initial_fetches += [e.encoded for e in self._all_coders
                                  if hasattr(e, 'encoded')]
        self._beam_size = beam_size
        self._beam_scoring_f = beam_scoring_f

    def get_executable(self, train=False, summaries=True):

        return RuntimeRnnExecutable(self._all_coders, self._decoder,
                                    self._initial_fetches,
                                    self._decoder.vocabulary,
                                    beam_size=self._beam_size,
                                    beam_scoring_f=self._beam_scoring_f,
                                    compute_loss=train)

    @property
    def loss_names(self) -> List[str]:
        return ["runtime_xent"]


class RuntimeRnnExecutable(Executable):
    """Run and ensemble the RNN decoder step by step."""

    def __init__(self, all_coders, decoder, initial_fetches, vocabulary,
                 beam_scoring_f, beam_size=1,
                 compute_loss=True):
        self._all_coders = all_coders
        self._decoder = decoder
        self._vocabulary = vocabulary
        self._initial_fetches = initial_fetches
        self._compute_loss = compute_loss
        self._beam_size = beam_size
        self._beam_scoring_f = beam_scoring_f

        self._to_exapand = [None]  # type: List[Option[BeamBatch]
        self._current_beam_batch = None
        self._expanded = []  # type: List[ExpandedBeamBatch]
        self._time_step = 0

        self.result = None  # type: Option[ExecutionResult]

    def next_to_execute(self) -> NextExecute:
        """Get the feedables and tensors to run.

        It takes a beam batch that should be expanded the next and preprare an
        additional feed_dict based on the hypotheses history.
        """

        if self.result is not None:
            raise Exception(
                "Nothing to execute, if there is already a result.")

        to_run = [self._decoder.train_logprobs[self._time_step]]

        self._current_beam_batch = self._to_exapand.pop()

        if self._current_beam_batch is not None:
            additional_feed_dict = {t: index for t, index in zip(
                self._decoder.train_inputs[1:],
                self._current_beam_batch.decoded.T)}
        else:
            additional_feed_dict = {}

        # at the end, we should compute loss
        if self._time_step == self._decoder.max_output - 1:
            if self._compute_loss:
                to_run.append(self._decoder.train_loss)
            else:
                to_run.append(tf.zeros([]))

        return self._all_coders, to_run, additional_feed_dict

    def collect_results(self, results: List[List[RunResult]]) -> None:
        """Process what the TF session returned.

        Only a single time step is always processed at once. First,
        distributions from all sessions are aggregated.

        """

        summed_logprobs = -np.inf
        for sess_result in results:
            summed_logprobs = np.logaddexp(summed_logprobs, sess_result[0])
        avg_logprobs = summed_logprobs - np.log(len(results))

        expanded_batch = ExpandedBeamBatch(self._current_beam_batch,
                                           avg_logprobs)
        self._expanded.append(expanded_batch)

        if not self._to_exapand:
            self._time_step += 1
            self._to_exapand = n_best(
                self._beam_size, self._expanded, self._beam_scoring_f)
            self._expanded = []

        if self._time_step == self._decoder.max_output:
            top_batch = self._to_exapand[-1].decoded.T
            decoded_tokens = self._vocabulary.vectors_to_sentences(top_batch)
            # loss = np.average([res[1] for res in results])
            self.result = ExecutionResult(
                outputs=decoded_tokens,
                losses=[],
                scalar_summaries=None,
                histogram_summaries=None,
                image_summaries=None
            )
