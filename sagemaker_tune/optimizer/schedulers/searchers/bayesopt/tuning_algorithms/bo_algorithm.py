from typing import List, NamedTuple, Tuple, Iterator, Optional
import logging
import numpy as np
import itertools

from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.datatypes.common \
    import Configuration
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.datatypes.hp_ranges \
    import HyperparameterRanges
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.models.model_transformer \
    import ModelStateTransformer
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.tuning_algorithms.base_classes \
    import NextCandidatesAlgorithm, CandidateGenerator, ScoringFunction, \
    LocalOptimizer, SurrogateModel
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.tuning_algorithms.bo_algorithm_components \
    import LBFGSOptimizeAcquisition
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.tuning_algorithms.common \
    import generate_unique_candidates, ExclusionList
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.utils.debug_log \
    import DebugLogPrinter
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.utils.duplicate_detector \
    import DuplicateDetector
from sagemaker_tune.optimizer.schedulers.utils.simple_profiler \
    import SimpleProfiler

logger = logging.getLogger(__name__)


class BayesianOptimizationAlgorithm(NamedTuple, NextCandidatesAlgorithm):
    """
    Core logic of the Bayesian optimization algorithm
    :param initial_candidates_generator: generator of candidates
    :param initial_scoring_function: scoring function used to rank the initial
        candidates.
        Note: If a batch is selected in one go (num_requested_candidates > 1,
        greedy_batch_selection = False), this function should encourage
        diversity among its top scorers. In general, greedy batch selection
        is recommended.
    :param num_initial_candidates: how many initial candidates to generate, if
        possible
    :param local_optimizer: local optimizer which starts from score minimizer.
        If a batch is selected in one go (not greedily), then local
        optimizations are started from the top num_requested_candidates ranked
        candidates (after scoring)
    :param pending_candidate_state_transformer: Once a candidate is selected, it
        becomes pending, and the state is transformed by appending information.
        This is done by the transformer.
        This is object is needed only if next_candidates goes through > 1 outer
        iterations (i.e., if greedy_batch_selection is True and
        num_requested_candidates > 1. Otherwise, None can be passed here.
        Note: Model updates (by the state transformer) for batch candidates beyond
        the first do not involve fitting hyperparameters, so they are usually
        cheap.
    :param exclusion_candidates: set of tuples, candidates that should not be
        returned, because they are already labeled, currently pending, or have
        failed
    :param num_requested_candidates: number of candidates to return
    :param greedy_batch_selection: If True and num_requested_candidates > 1, we
        generate, order, and locally optimize for each single candidate to be
        selected. Otherwise (False), this is done just once, and
        num_requested_candidates are extracted in one go.
        Note: If this is True, pending_candidate_state_transformer is needed.
    :param duplicate_detector: used to make sure no candidates equal to already
        evaluated ones is returned
    :param profiler: If given, this is used for profiling parts in the code
    :param sample_unique_candidates: If True, we check that initial candidates
        sampled at random are unique and disjoint from the exclusion list.
        See below.
    :param debug_log: If a DebugLogPrinter is passed here, it is used to write
        log messages

    """

    initial_candidates_generator: CandidateGenerator
    initial_candidates_scorer: ScoringFunction
    num_initial_candidates: int
    local_optimizer: LocalOptimizer
    pending_candidate_state_transformer: Optional[ModelStateTransformer]
    exclusion_candidates: ExclusionList
    num_requested_candidates: int
    greedy_batch_selection: bool
    duplicate_detector: DuplicateDetector
    profiler: SimpleProfiler = None
    sample_unique_candidates: bool = False
    debug_log: Optional[DebugLogPrinter] = None

    # Note: For greedy batch selection (num_outer_iterations > 1), the
    # underlying SurrrogateModel changes with each new pending candidate. The
    # model changes are managed by pending_candidate_state_transformer. The
    # model has to be passed to both initial_candidates_scorer and
    # local_optimizer.
    def next_candidates(self) -> List[Configuration]:
        if self.greedy_batch_selection:
            # Select batch greedily, one candidate at a time, updating the
            # model in between
            num_outer_iterations = self.num_requested_candidates
            num_inner_candidates = 1
        else:
            # Select batch in one go
            num_outer_iterations = 1
            num_inner_candidates = self.num_requested_candidates
        assert num_outer_iterations == 1 or self.pending_candidate_state_transformer, \
            "Need pending_candidate_state_transformer for greedy batch selection"
        candidates = []
        just_added = True
        model = None  # SurrogateModel, if num_outer_iterations > 1
        for outer_iter in range(num_outer_iterations):
            if just_added:
                if self.exclusion_candidates.config_space_exhausted():
                    logger.warning(
                        "All entries of finite config space (size " + \
                        f"{self.exclusion_candidates.configspace_size}) have been selected. Returning " + \
                        f"{len(candidates)} configs instead of {self.num_requested_candidates}")
                    break
                just_added = False
            inner_candidates = self._get_next_candidates(
                num_inner_candidates, model=model)
            candidates.extend(inner_candidates)
            if outer_iter < num_outer_iterations - 1 and len(inner_candidates) > 0:
                just_added = True
                # This is not the last outer iteration
                for cand in inner_candidates:
                    self.exclusion_candidates.add(cand)
                # State transformer is used to produce new model
                # Note: We suppress fit_hyperpars for models obtained during
                # batch selection
                for candidate in inner_candidates:
                    self.pending_candidate_state_transformer.append_candidate(
                        candidate)
                model = self.pending_candidate_state_transformer.model(
                    skip_optimization=True)
            if len(inner_candidates) < num_inner_candidates and \
                    len(candidates) < self.num_requested_candidates:
                logger.warning(
                    "All entries of finite config space (size " + \
                    f"{self.exclusion_candidates.configspace_size}) have been selected. Returning " + \
                    f"{len(candidates)} configs instead of {self.num_requested_candidates}")
                break

        return candidates

    def _get_next_candidates(self, num_candidates: int,
                             model: Optional[SurrogateModel]):
        # generate a random candidates among which to pick the ones to be
        # locally optimized
        logger.info("BayesOpt Algorithm: Generating initial candidates.")
        if self.profiler is not None:
            self.profiler.push_prefix('nextcand')
            self.profiler.start('all')
            self.profiler.start('genrandom')
        if self.sample_unique_candidates:
            # This can be expensive, depending on what type Candidate is
            initial_candidates = generate_unique_candidates(
                self.initial_candidates_generator,
                self.num_initial_candidates, self.exclusion_candidates)
        else:
            # Will not return candidates in `exclusion_candidates`, but there
            # can be duplicates
            initial_candidates = \
                self.initial_candidates_generator.generate_candidates_en_bulk(
                    self.num_initial_candidates,
                    exclusion_list=self.exclusion_candidates)
        if self.profiler is not None:
            self.profiler.stop('genrandom')
            self.profiler.start('scoring')
        logger.info("BayesOpt Algorithm: Scoring (and reordering) candidates.")
        if self.debug_log is not None:
            candidates_and_scores = _order_candidates(
                initial_candidates, self.initial_candidates_scorer,
                model=model, with_scores=True)
            initial_candidates = [cand for score, cand in candidates_and_scores]
            config = initial_candidates[0]
            top_scores = np.array([x for x, _ in candidates_and_scores[:5]])
            self.debug_log.set_init_config(config, top_scores)
        else:
            initial_candidates = _order_candidates(
                initial_candidates, self.initial_candidates_scorer,
                model=model)
        if self.profiler is not None:
            self.profiler.stop('scoring')
            self.profiler.start('localsearch')
        candidates_with_optimization = _lazily_locally_optimize(
            initial_candidates, self.local_optimizer,
            hp_ranges=self.exclusion_candidates.hp_ranges, model=model)
        logger.info("BayesOpt Algorithm: Selecting final set of candidates.")
        if self.debug_log is not None and \
                isinstance(self.local_optimizer, LBFGSOptimizeAcquisition):
            # We would like to get num_evaluations from the first run (usually
            # the only one). This requires peeking at the first entry of the
            # iterator
            peek = candidates_with_optimization.__next__()
            self.debug_log.set_num_evaluations(
                self.local_optimizer.num_evaluations)
            candidates_with_optimization = itertools.chain(
                [peek], candidates_with_optimization)
        candidates = _pick_from_locally_optimized(
            candidates_with_optimization, self.exclusion_candidates,
            num_candidates, self.duplicate_detector)
        if self.profiler is not None:
            self.profiler.stop('localsearch')
            self.profiler.stop('all')
            self.profiler.pop_prefix()  # nextcand
        return candidates


def _order_candidates(
        candidates: List[Configuration],
        scoring_function: ScoringFunction,
        model: Optional[SurrogateModel],
        with_scores: bool = False):
    if len(candidates) == 0:
        return []
    # scored in batch as this can be more efficient
    scores = scoring_function.score(candidates, model=model)
    sorted_list = sorted(zip(scores, candidates), key=lambda x: x[0])
    if with_scores:
        return sorted_list
    else:
        return [cand for score, cand in sorted_list]


def _lazily_locally_optimize(
        candidates: List[Configuration],
        local_optimizer: LocalOptimizer, hp_ranges: HyperparameterRanges,
        model: Optional[SurrogateModel]) -> Iterator[Tuple[Configuration, Configuration]]:
    """
    Due to local deduplication we do not know in advance how many candidates
    we have to locally optimize, hence this helper to create a lazy generator
    of locally optimized candidates.
    Note that `candidates` may contain duplicates, but such are skipped here.
    """
    considered_already = ExclusionList.empty_list(hp_ranges)
    for cand in candidates:
        if not considered_already.contains(cand):
            considered_already.add(cand)
            yield cand, local_optimizer.optimize(cand, model=model)


# Note: If duplicate_detector is at least DuplicateDetectorIdentical, it will
# filter out candidates in exclusion_candidates here. Such can in principle
# arise if sample_unique_candidates == False.
# This does not work if duplicate_detector is DuplicateDetectorNoDetection.
def _pick_from_locally_optimized(
        candidates_with_optimization: Iterator[Tuple[Configuration, Configuration]],
        exclusion_candidates: ExclusionList,
        num_candidates: int,
        duplicate_detector: DuplicateDetector) -> List[Configuration]:
    updated_excludelist = exclusion_candidates.copy()
    result = []
    for original_candidate, optimized_candidate in candidates_with_optimization:
        insert_candidate = None
        optimized_is_duplicate = duplicate_detector.contains(
            updated_excludelist, optimized_candidate)
        if optimized_is_duplicate:
            # in the unlikely case that the optimized candidate ended at a
            # place that caused a duplicate we try to return the original instead
            original_also_duplicate = duplicate_detector.contains(
                updated_excludelist, original_candidate)
            if not original_also_duplicate:
                insert_candidate = original_candidate
        else:
            insert_candidate = optimized_candidate
        if insert_candidate is not None:
            result.append(insert_candidate)
            updated_excludelist.add(insert_candidate)
        if len(result) == num_candidates:
            break

    return result
