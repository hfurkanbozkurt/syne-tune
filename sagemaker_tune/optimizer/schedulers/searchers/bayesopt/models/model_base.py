from typing import List
import numpy as np
import logging

from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.datatypes.tuning_job_state \
    import TuningJobState
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.tuning_algorithms.base_classes \
    import SurrogateModel

logger = logging.getLogger(__name__)


class BaseSurrogateModel(SurrogateModel):
    """
    Base class for (most) SurrogateModel implementations, provides common code

    """
    def __init__(
            self, state: TuningJobState, active_metric: str = None):
        super().__init__(state, active_metric)
        self._current_best = None

    def predict_mean_current_candidates(self) -> List[np.ndarray]:
        """
        Returns the predictive mean (signal with key 'mean') at all current candidate
        locations (both state.candidate_evaluations and state.pending_evaluations).

        If the hyperparameters of the surrogate model are being optimized (e.g.,
        by empirical Bayes), the returned list has length 1. If its
        hyperparameters are averaged over by MCMC, the returned list has one
        entry per MCMC sample.

        :return: List of predictive means
        """
        candidates, _ = self.state.observed_data_for_metric(self.active_metric)
        candidates += self.state.pending_candidates
        assert len(candidates) > 0, \
            "Cannot predict means at current candidates with no candidates at all"
        inputs = self.state.hp_ranges.to_ndarray_matrix(candidates)
        all_means = []
        # Loop over MCMC samples (if any)
        for prediction in self.predict(inputs):
            means = prediction['mean']
            if means.ndim == 1:  # In case of no fantasizing
                means = means.reshape((-1, 1))
            all_means.append(means)
        return all_means

    def current_best(self) -> List[np.ndarray]:
        if self._current_best is None:
            all_means = self.predict_mean_current_candidates()
            result = [np.min(means, axis=0) for means in all_means]
            self._current_best = result
        return self._current_best

    def current_best_filter_candidates(self, candidates):
        """
        In some subclasses, 'current_best' is not computed over all (observed
        and pending) candidates: they need to implement this filter.

        """
        return candidates  # Default: No filtering
