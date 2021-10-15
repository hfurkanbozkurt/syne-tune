import logging
from pathlib import Path

from ray.tune.schedulers import AsyncHyperBandScheduler
from ray.tune.suggest.skopt import SkOptSearch
import numpy as np

from sagemaker_tune.backend.local_backend import LocalBackend
from sagemaker_tune.optimizer.schedulers.ray_scheduler import RayTuneScheduler
from sagemaker_tune.tuner import Tuner
from sagemaker_tune.search_space import randint
from sagemaker_tune.stopping_criterion import StoppingCriterion

if __name__ == '__main__':
    logging.getLogger().setLevel(logging.DEBUG)

    random_seed = 31415927
    max_steps = 100
    n_workers = 4

    config_space = {
        "steps": max_steps,
        "width": randint(0, 20),
        "height": randint(-100, 100)
    }
    entry_point = str(
        Path(__file__).parent / "training_scripts" / "height_example" /
        "train_height.py")
    mode = "min"
    metric = "mean_loss"

    # Local back-end
    backend = LocalBackend(entry_point=entry_point)

    # Hyperband scheduler with SkOpt searcher
    np.random.seed(random_seed)
    ray_searcher = SkOptSearch()
    ray_searcher.set_search_properties(
        mode=mode, metric=metric,
        config=RayTuneScheduler.convert_config_space(config_space))

    ray_scheduler = AsyncHyperBandScheduler(
        max_t=max_steps,
        time_attr="step",
        mode=mode,
        metric=metric)

    scheduler = RayTuneScheduler(
        config_space=config_space,
        ray_scheduler=ray_scheduler,
        ray_searcher=ray_searcher)

    stop_criterion = StoppingCriterion(max_wallclock_time=60)
    tuner = Tuner(
        backend=backend,
        scheduler=scheduler,
        stop_criterion=stop_criterion,
        n_workers=n_workers,
    )

    tuner.run()
