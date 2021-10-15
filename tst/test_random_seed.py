import tempfile
from typing import Dict
import pytest
import itertools
import logging

from sagemaker_tune.backend.local_backend import LocalBackend
from sagemaker_tune.optimizer.schedulers.hyperband import HyperbandScheduler
from sagemaker_tune.optimizer.schedulers.fifo import FIFOScheduler
from sagemaker_tune.tuner import Tuner
from sagemaker_tune.tuner_callback import TunerCallback
from sagemaker_tune.backend.trial_status import Trial
from sagemaker_tune.optimizer.scheduler import TrialScheduler
from sagemaker_tune.search_space import randint
from sagemaker_tune.util import script_height_example_path
from tst.util_test import temporary_local_backend


class StoreConfigCallback(TunerCallback):
    def __init__(self):
        super().__init__()
        self._keys = ['height', 'width']  # Only these change
        self.configs = []
        self._configs_set = set()

    def _reduce(self, config):
        return {k: config[k] for k in self._keys}

    def _to_tuple(self, config):
        return tuple(config[k] for k in self._keys)

    def on_trial_result(self, trial: Trial, status: str, result: Dict, decision: str):
        config = self._reduce(trial.config)
        key = self._to_tuple(config)
        if key not in self._configs_set:
            self.configs.append(config)
            self._configs_set.add(key)


_parameterizations = list(itertools.product(
    ['fifo', 'hyperband_stopping', 'hyperband_promotion'],
    [382378624]))

@pytest.mark.parametrize(
    "scheduler, random_seed", _parameterizations)
def test_scheduler(scheduler, random_seed):
    max_steps = 5
    # Note: For num_workers > 1, the ordering of configs may change even if the
    # random seed stays the same
    num_workers = 1

    config_space = {
        "steps": max_steps,
        "width": randint(0, 20),
        "height": randint(-100, 100),
        "sleep_time": 0.01
    }

    entry_point = script_height_example_path()
    metric = 'mean_loss'
    mode = 'min'

    backend = temporary_local_backend(entry_point=entry_point)

    search_options = {'debug_log': False}
    kwargs = dict(
        searcher='random',
        search_options=search_options,
        mode=mode,
        metric=metric,
        random_seed=random_seed)
    if scheduler == 'fifo':
        myscheduler1 = FIFOScheduler(config_space, **kwargs)
        myscheduler2 = FIFOScheduler(config_space, **kwargs)
    else:
        prefix = 'hyperband_'
        assert scheduler.startswith(prefix)
        sch_type = scheduler[len(prefix):]
        kwargs = dict(
            kwargs,
            max_t=max_steps,
            type=sch_type,
            resource_attr='epoch')
        myscheduler1 = HyperbandScheduler(config_space, **kwargs)
        myscheduler2 = HyperbandScheduler(config_space, **kwargs)

    logging.getLogger('sagemaker_tune.tuner').setLevel(logging.ERROR)

    stop_criterion = lambda status: status.wallclock_time > 0.5
    callback1 = StoreConfigCallback()
    tuner1 = Tuner(
        backend=backend,
        scheduler=myscheduler1,
        sleep_time=0.02,
        n_workers=num_workers,
        stop_criterion=stop_criterion,
    )
    tuner1.run(
        callbacks=[callback1],
    )

    backend = temporary_local_backend(entry_point=entry_point)
    callback2 = StoreConfigCallback()
    tuner2 = Tuner(
        backend=backend,
        scheduler=myscheduler2,
        sleep_time=0.02,
        n_workers=num_workers,
        stop_criterion=stop_criterion,
    )
    tuner2.run(
        callbacks=[callback2],
    )
    configs1 = callback1.configs
    configs2 = callback2.configs
    len1 = len(configs1)
    len2 = len(configs2)
    if len1 != len2:
        # Different lengths can happen even for the same random seed
        print(f"scheduler = {scheduler}, random_seed = {random_seed}: "
              f"len1 = {len1}, len2 = {len2}")
        clen = min(len1, len2)
        configs1 = configs1[:clen]
        configs2 = configs2[:clen]
    if configs1 != configs2:
        parts = [
            f"scheduler = {scheduler}, random_seed = {random_seed}",
            f"configs1[{len1}] --- configs2[{len2}]"]
        for i, (c1, c2) in enumerate(zip(configs1, configs2)):
            parts.append(f"{i}: {c1 == c2}: {c1} --- {c2}")
        raise AssertionError('\n'.join(parts))
