import argparse
import json
import logging
import os
import time
from pathlib import Path

from sagemaker_tune.report import Reporter


report = Reporter()


def load_checkpoint(checkpoint_path: Path):
    with open(checkpoint_path, "r") as f:
        return json.load(f)


def save_checkpoint(checkpoint_path: Path, epoch: int, value: float):
    os.makedirs(checkpoint_path.parent, exist_ok=True)
    with open(checkpoint_path, "w") as f:
        json.dump({"last_epoch": epoch, "last_value": value}, f)


if __name__ == '__main__':
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument('--num-epochs', type=int, required=True)
    parser.add_argument('--multiplier', type=float, default=1)
    parser.add_argument('--sleep-time', type=float, default=0.1)

    # convention the path where to serialize and deserialize is given as smt_checkpoint_dir
    parser.add_argument('--smt_checkpoint_dir', type=str)

    args, _ = parser.parse_known_args()

    num_epochs = args.num_epochs
    checkpoint_path = None
    start_epoch = 0
    current_value = 0
    if args.smt_checkpoint_dir is not None:
        checkpoint_path = Path(args.smt_checkpoint_dir) / "checkpoint.json"
        if checkpoint_path.exists():
            state = load_checkpoint(checkpoint_path)
            logging.info(f"resuming from previous checkpoint {state}")
            start_epoch = state["last_epoch"] + 1
            current_value = state["last_value"]

    # write dumb values for loss to illustrate sagemaker ability to retrieve metrics
    # should be replaced by your algorithm
    for current_epoch in range(start_epoch, num_epochs):
        current_value = (current_value + 1) * args.multiplier
        report(train_acc=current_value, step=current_epoch)
        if checkpoint_path is not None:
            save_checkpoint(checkpoint_path, current_epoch, current_value)
        time.sleep(args.sleep_time)
