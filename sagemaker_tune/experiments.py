import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Callable

import pandas as pd

from dataclasses import dataclass

from sagemaker_tune.constants import SMT_TUNER_TIME, SMT_TUNER_CREATION_TIMESTAMP
from sagemaker_tune.tuner import Tuner
from sagemaker_tune.util import experiment_path


@dataclass
class ExperimentResult:
    name: str
    results: pd.DataFrame
    metadata: Dict
    tuner: Tuner

    def __str__(self):
        res = f"Experiment {self.name}"
        if self.results is not None:
            res += f" contains {len(self.results)} evaluations from {len(self.results.trial_id.unique())} trials"
        if self.tuner is not None:
            metrics = ", ".join(self.tuner.scheduler.metric_names())
            res += f" when tuning {metrics} on {self.entrypoint_name()} with {self.scheduler_name()}."
        return res

    def creation_date(self):
        return datetime.fromtimestamp(self.metadata[SMT_TUNER_CREATION_TIMESTAMP])

    def plot(self, **plt_kwargs):
        import matplotlib.pyplot as plt

        scheduler = self.tuner.scheduler
        metric = self.metric_name()
        df = self.results
        df = df.sort_values(SMT_TUNER_TIME)
        x = df.loc[:, SMT_TUNER_TIME]
        y = df.loc[:, metric].cummax() if self.metric_mode() == "max" else df.loc[:, metric].cummin()
        plt.plot(x, y, **plt_kwargs)
        plt.xlabel("wallclock time")
        plt.ylabel(metric)
        plt.title(self.entrypoint_name() + "-" + scheduler.__class__.__name__)

    def metric_mode(self):
        return self.tuner.scheduler.metric_mode()

    def metric_name(self) -> str:
        return self.tuner.scheduler.metric_names()[0]

    def entrypoint_name(self) -> str:
        return Path(self.tuner.backend.entrypoint_path()).stem

    def scheduler_name(self) -> str:
        return self.tuner.scheduler.__class__.__name__


def load_experiment(tuner_name: str) -> ExperimentResult:
    path = experiment_path(tuner_name)

    metadata_path = path / "metadata.json"
    try:
        with open(metadata_path, "r") as f:
            metadata = json.load(f)
    except FileNotFoundError:
        metadata = None
    try:
        if (path / "results.csv.zip").exists():
            results = pd.read_csv(path / "results.csv.zip")
        else:
            results = pd.read_csv(path / "results.csv")
    except FileNotFoundError:
        results = None
    try:
        tuner = Tuner.load(path)
    except FileNotFoundError:
        tuner = None
    except Exception:
        tuner = None

    return ExperimentResult(
        name=tuner.name if tuner is not None else path.stem,
        results=results,
        tuner=tuner,
        metadata=metadata,
    )


def list_experiments(experiment_filter: Callable[[ExperimentResult], bool] = None) -> List[ExperimentResult]:
    res = []
    for path in experiment_path().rglob("*/results.csv*"):
        exp = load_experiment(path.parent.name)
        if experiment_filter is None or experiment_filter(exp):
            if exp.results is not None and exp.tuner is not None and exp.metadata is not None:
                res.append(exp)
    return sorted(res, key=lambda exp: exp.metadata.get(SMT_TUNER_CREATION_TIMESTAMP, 0), reverse=True)



# TODO: Use conditional imports, in order not to fail if dependencies are not
# installed
def scheduler_name(scheduler):
    from sagemaker_tune.optimizer.schedulers.fifo import FIFOScheduler

    if isinstance(scheduler, FIFOScheduler):
        scheduler_name = f"SMT-{scheduler.__class__.__name__}"
        searcher = scheduler.searcher.__class__.__name__
        return "-".join([scheduler_name, searcher])
    else:
        from sagemaker_tune.optimizer.schedulers.ray_scheduler import RayTuneScheduler

        if isinstance(scheduler, RayTuneScheduler):
            scheduler_name = f"Ray-{scheduler.scheduler.__class__.__name__}"
            searcher = scheduler.searcher.__class__.__name__
            return "-".join([scheduler_name, searcher])
        else:
            return scheduler.__class__.__name__


def load_experiments_df(experiment_filter: Callable[[ExperimentResult], bool] = None) -> pd.DataFrame:
    """
    :param experiment_filter: only experiment where the filter is True are kept, default to None and returns everything.
    :return: a dataframe that contains all evaluations reported by tuners according to the filter given.
    The columns contains trial-id, hyperparameter evaluated, metrics observed by `report`:
     metrics collected automatically by sagemaker-tune:
     `smt_worker_time` (indicating time spent in the worker when report was seen)
     `time` (indicating wallclock time measured by the tuner)
     `decision` decision taken by the scheduler when observing the result
     `status` status of the trial that was shown to the tuner
     `config_{xx}` configuration value for the hyperparameter {xx}
     `tuner_name` named passed when instantiating the Tuner
     `entry_point_name`/`entry_point_path` name and path of the entry point that was tuned
    """
    dfs = []
    for experiment in list_experiments(experiment_filter=experiment_filter):
        assert experiment.tuner is not None
        assert experiment.results is not None
        assert experiment.metadata is not None

        df = experiment.results
        df["tuner_name"] = experiment.name
        df["scheduler"] = scheduler_name(experiment.tuner.scheduler)
        df["backend"] = experiment.tuner.backend.__class__.__name__
        df["entry_point"] = experiment.tuner.backend.entrypoint_path()
        df["entry_point_name"] = Path(experiment.tuner.backend.entrypoint_path()).stem

        metrics = experiment.tuner.scheduler.metric_names()
        if len(metrics) == 1:
            df["metric"] = experiment.tuner.scheduler.metric_names()[0]
        else:
            # assume error is always the first
            df["metric"] = experiment.tuner.scheduler.metric_names()[0]

        scheduler_mode = experiment.tuner.scheduler.metric_mode()
        if isinstance(scheduler_mode, str):
            df["mode"] = scheduler_mode
        else:
            df["mode"] = scheduler_mode[0]
        dfs.append(df)
    return pd.concat(dfs, ignore_index=True)


def split_per_task(df) -> Dict[str, pd.DataFrame]:
    # split by endpoint script
    dfs = {}
    for entry_point in df.entry_point_name.unique():
        df_entry_point = df.loc[df.entry_point_name == entry_point, :].dropna(axis=1, how='all')
        if "config_dataset_name" in df_entry_point:
            for dataset in df_entry_point.loc[:, "config_dataset_name"].unique():
                dataset_mask = df_entry_point.loc[:, "config_dataset_name"] == dataset
                dfs[f"{entry_point}-{dataset}"] = df_entry_point.loc[dataset_mask, :]
        else:
            dfs[entry_point] = df_entry_point
    return dfs


if __name__ == '__main__':
    for exp in list_experiments():
        if exp.results is not None:
            print(exp)

