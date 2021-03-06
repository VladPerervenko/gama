from functools import partial
import logging
import math
from typing import List, Optional, Dict, Tuple, Any

import pandas as pd
import stopit

from gama.genetic_programming.operator_set import OperatorSet
from gama.search_methods.base_search import BaseSearch
from gama.logging.machine_logging import log_event
from gama.utilities.generic.async_evaluator import AsyncEvaluator
from gama.genetic_programming.components.individual import Individual

log = logging.getLogger(__name__)
ASHA_LOG_TOKEN = "ASHA"


class AsynchronousSuccessiveHalving(BaseSearch):
    """ Asynchronous Halving Algorithm by Li et al.

    paper: https://arxiv.org/abs/1810.05934

    Parameters
    ----------
    reduction_factor: int, optional (default=3)
        Reduction factor of candidates between each rung.
    minimum_resource: int, optional (default=100)
        Number of samples to use in the lowest rung.
    maximum_resource: int, optional (default=number of samples in the dataset)
        Number of samples to use in the top rung.
        This should not exceed the number of samples in the data.
    minimum_early_stopping_rate: int (default=1)
        Number of lowest rungs to skip.
    """

    def __init__(
        self,
        reduction_factor: Optional[int] = None,
        minimum_resource: Optional[int] = None,
        maximum_resource: Optional[int] = None,
        minimum_early_stopping_rate: Optional[int] = None,
    ):
        super().__init__()
        # maps hyperparameter -> (set value, default)
        self._hyperparameters: Dict[str, Tuple[Any, Any]] = dict(
            reduction_factor=(reduction_factor, 3),
            minimum_resource=(minimum_resource, 100),
            maximum_resource=(maximum_resource, 100_000),
            minimum_early_stopping_rate=(minimum_early_stopping_rate, 1),
        )
        self.output = []

    def dynamic_defaults(self, x: pd.DataFrame, y: pd.DataFrame, time_limit: float):
        # `maximum_resource` is the number of samples used in the highest rung.
        # this typically should be the number of samples in the (training) dataset.
        self._overwrite_hyperparameter_default("maximum_resource", len(y))

    def search(self, operations: OperatorSet, start_candidates: List[Individual]):
        self.output = asha(
            operations, start_candidates=start_candidates, **self.hyperparameters
        )


def asha(
    operations: OperatorSet,
    start_candidates: List[Individual],
    reduction_factor: int = 3,
    minimum_resource: int = 100,
    maximum_resource: int = 100_000,
    minimum_early_stopping_rate: int = 1,
    max_full_evaluations: Optional[int] = None,
) -> List[Individual]:
    """ Asynchronous Halving Algorithm by Li et al.

    paper: https://arxiv.org/abs/1810.05934

    Parameters
    ----------
    operations: OperatorSet
        An operator set with `evaluate` and `individual` functions.
    start_candidates: List[Individual]
        A list which contains the set of best found individuals during search.
    reduction_factor: int (default=3)
        Reduction factor of candidates between each rung.
    minimum_resource: int (default=100)
        Number of samples to use in the lowest rung.
    maximum_resource: int (default=100_000)
        Number of samples to use in the top rung.
        This should not exceed the number of samples in the data.
    minimum_early_stopping_rate: int (default=1)
        Number of lowest rungs to skip.
    max_full_evaluations: Optional[int] (default=None)
        Maximum number of individuals to evaluate on the max rung (i.e. on all data).
        If None, the algorithm will be run indefinitely.

    Returns
    -------
    List[Individual]
        Individuals of the highest rung in which
        at least one individual has been evaluated.
    """
    evaluate = partial(evaluate_on_rung, evaluate_individual=operations.evaluate)

    # Note that here we index the rungs by all possible rungs (0..ceil(log_eta(R/r))),
    # and ignore the first minimum_early_stopping_rate rungs.
    # This contrasts the paper where rung 0 refers to the first used one.
    max_rung = math.ceil(
        math.log(maximum_resource / minimum_resource, reduction_factor)
    )
    rungs = range(minimum_early_stopping_rate, max_rung + 1)
    rung_resources = {
        rung: min(minimum_resource * (reduction_factor ** rung), maximum_resource)
        for rung in rungs
    }

    # Highest rungs first is how we typically iterate them
    # Should we just use lists of lists/heaps instead?
    rung_individuals: Dict[int, List[Tuple[float, Individual]]] = {
        rung: [] for rung in reversed(rungs)
    }
    promoted_individuals: Dict[int, List[Individual]] = {
        rung: [] for rung in reversed(rungs)
    }

    def get_job():
        for rung, individuals in list(rung_individuals.items())[1:]:
            # This is not in the paper code but is derived from fig 2b
            n_to_promote = math.floor(len(individuals) / reduction_factor)
            if n_to_promote - len(promoted_individuals[rung]) > 0:
                # Problem: equal loss falls back on comparison of individual
                not_promoted = set(individuals) - set(promoted_individuals[rung])
                if len(not_promoted) > 0:
                    to_promote = max(not_promoted, key=lambda i: i[0])
                    promoted_individuals[rung].append(to_promote)
                    return to_promote[1], rung + 1

        if start_candidates is not None and len(start_candidates) > 0:
            return start_candidates.pop(), minimum_early_stopping_rate
        else:
            return operations.individual(), minimum_early_stopping_rate

    try:
        with AsyncEvaluator() as async_:
            log.info("ASHA start")

            def start_new_job():
                individual, rung = get_job()
                time_penalty = rung_resources[rung] / max(rung_resources.values())
                async_.submit(
                    evaluate,
                    individual,
                    rung,
                    rung == max(rungs),
                    subsample=rung_resources[rung],
                    timeout=(10 + (time_penalty * 600)),
                )

            for _ in range(8):
                start_new_job()

            while (max_full_evaluations is None) or (
                len(rung_individuals[max_rung]) < max_full_evaluations
            ):
                future = operations.wait_next(async_)
                if future.result is not None:
                    evaluation, loss, rung, _ = future.result
                    individual = evaluation.individual
                    rung_individuals[rung].append((loss, individual))
                    # Due to `evaluate` returning additional information (like rung),
                    # evaluations are not automatically logged, so we do it here.
                    log_event(
                        log,
                        ASHA_LOG_TOKEN,
                        rung,
                        individual.fitness.wallclock_time,
                        individual.fitness.values,
                        individual._id,
                        individual.pipeline_str(),
                    )

                start_new_job()

            highest_rung_reached = max(rungs)
    except stopit.TimeoutException:
        log.info("ASHA ended due to timeout.")
        reached_rungs = (rung for rung, inds in rung_individuals.items() if inds != [])
        highest_rung_reached = max(reached_rungs)
        if highest_rung_reached != max(rungs):
            raise RuntimeWarning("Highest rung not reached.")
    finally:
        for rung, individuals in rung_individuals.items():
            log.info(f"[{len(individuals)}] {rung}")
        return list(map(lambda p: p[1], rung_individuals[highest_rung_reached]))


def evaluate_on_rung(individual, rung, report, evaluate_individual, *args, **kwargs):
    evaluation = evaluate_individual(individual, *args, **kwargs)
    return evaluation, evaluation.score[0], rung, report
