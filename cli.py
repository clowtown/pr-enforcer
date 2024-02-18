import logging
import os
import sys
from collections import defaultdict

import click
import github
from github import Github
from github.CheckRun import CheckRun
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_delay, wait_fixed

logger = logging.getLogger("pr-enforcer")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)


class ConditionalFormatter(logging.Formatter):
    """Logger to allow github annotations to be found which require no formatting"""

    def format(self, record):
        if hasattr(record, "simple") and record.simple:
            return record.getMessage()
        else:
            return logging.Formatter.format(self, record)


formatter = ConditionalFormatter("%(message)s")
handler.setFormatter(formatter)
if logger.hasHandlers():
    logger.handlers.clear()
logger.propagate = False
logger.addHandler(handler)


def update_logging(debug: bool):
    try:
        level = logging.DEBUG if debug else logging.INFO
        logger.setLevel(level)  # pylint: disable=no-member
        logger.debug(f"pr-enforcer log level set to: {level}")
    except AttributeError:
        logger.exception("failed using behave logging level for pr-enforcer. defaulting to INFO")


class BColors:
    Red = "\033[91m"
    Green = "\033[92m"
    Teal = "\033[94m"
    Grey = "\033[90m"
    ResetAll = "\033[0m"
    Debug = "\33[35m"
    Separator = "\33[33m"


class Status:
    queued = "queued"
    in_progress = "in_progress"
    completed = "completed"

    @staticmethod
    def complete() -> list[str]:
        return [Status.completed]

    @staticmethod
    def incomplete() -> list[str]:
        return [Status.queued, Status.in_progress]


class Conclusion:
    """These are listed in order of lifeline
    The result of a completed step after continue-on-error is applied.
    Possible values are success, failure, cancelled, or skipped.
    When a continue-on-error step fails, the outcome is failure, but the final conclusion is success.
    """

    action_required = "action_required"
    cancelled = "cancelled"
    timed_out = "timed_out"
    failure = "failure"
    failed = "failed"  # GH docs do not list this, but it is witnessed
    neutral = "neutral"
    skipped = "skipped"
    stale = "stale"
    startup_failure = "startup_failure"
    success = "success"

    @staticmethod
    def ignored() -> list[str]:
        return [Conclusion.neutral, Conclusion.skipped]

    @staticmethod
    def fail() -> list[str]:
        return [
            Conclusion.action_required,
            Conclusion.cancelled,
            Conclusion.timed_out,
            Conclusion.failure,
            Conclusion.failed,
            Conclusion.stale,
            Conclusion.startup_failure,
        ]

    @staticmethod
    def succeeded() -> list[str]:
        return [Conclusion.success]


def filter_and_log_by_conclusion(
    complete: list[CheckRun], conclusions: list[str], color: str, verb: str, marker: str
) -> list:
    runs = list(filter(lambda _run: _run.conclusion in conclusions, complete))
    logger.info(f"{color}{verb} of {len(runs)} workflows{BColors.ResetAll}")
    for run in runs:
        logger.debug(f"{color}{marker}{BColors.ResetAll}{run.name}")
    return runs


def filter_and_log_by_status(runs: list[CheckRun], statuses: list[str], color: str, verb: str, marker: str) -> list:
    runs = list(filter(lambda _run: _run.status in statuses, runs))
    logger.info(f"{color}{verb} of {len(runs)} workflows{BColors.ResetAll}")
    for run in runs:
        logger.debug(f"{color}{marker}{BColors.ResetAll}{run.name}")
    return runs


def reduce_to_latest_runs(check_runs: list[CheckRun]) -> list[CheckRun]:
    groups = defaultdict(list)
    for run in check_runs:
        groups[run.name].append(run)
    logger.debug(f"{BColors.ResetAll}Logging Run Check counts grouped{BColors.Debug}")
    logger.debug(str({k: len(v) for k, v in groups.items()}))
    latest_runs = []
    for _name, runs in groups.items():
        runs.sort(key=lambda vs: vs.id, reverse=True)
        latest_runs.append(runs[0])
    logger.debug(f"{BColors.ResetAll}Logging Latest Checks{BColors.Debug}")
    for run in latest_runs:
        logger.debug(f"{BColors.Debug}{run.name} is {run.status} with conclusion {run.conclusion}")
    return latest_runs


def log_summary(runs: list[CheckRun]):
    table = "\n|  Run Name | Status | Conclusion |"
    table += "\n|--------|--------|--------|"
    for run in runs:
        table += f"\n|{run.name}|{run.status}|{run.conclusion}|"
    table += "\n"
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            print(table, file=fh)  # noqa: T201, T001
    else:
        logger.debug(table)


class Enforcer(Exception):
    ...


@click.command()
@click.option("--token", required=True, type=str, help="github token")
@click.option("--repository", required=True, type=str, help="github repo")
@click.option("--branch", required=True, type=str, help="github branch")
@click.option("--interval", required=True, type=int, help="Interval or period in seconds to poll GitHub Check Runs")
@click.option("--timeout", required=True, type=int, help="Timeout in seconds to poll GitHub Check Runs")
@click.option("--name", help="Current Run name")
@click.option("--ignore", help="GitHub checks that should be ignored (default ignores the current job)")
@click.option("--exhaustive", is_flag=True, show_default=True, default=False, help="do not fail fast")
@click.option("--debug", is_flag=True, show_default=True, default=False, help="enable debug logs")
def hello(token, repository, branch, interval, timeout, name, ignore, exhaustive, debug):
    logger.info("Using config:")
    logger.info("{key:>10} {value}".format(key="repository", value=repository))
    logger.info("{key:>10} {value}".format(key="branch", value=branch))
    logger.info("{key:>10} {value}".format(key="interval", value=interval))
    logger.info("{key:>10} {value}".format(key="timeout", value=timeout))
    logger.info("{key:>10} {value}".format(key="name", value=name))
    logger.info("{key:>10} {value}".format(key="ignore", value=ignore))
    logger.info("{key:>10} {value}".format(key="exhaustive", value=exhaustive))
    logger.info("{key:>10} {value}".format(key="debug", value=debug))
    update_logging(debug=bool(debug))
    user_ignore = list(map(str.strip, ignore.split(",")))
    logger.debug(f"{BColors.Debug}User Ignore:")
    [logger.debug(f"{ui}") for ui in user_ignore]
    logger.debug(f"{BColors.ResetAll}")

    # Prevent infinite loop, remove this jobs name from the monitor list
    user_ignore.append(name)

    logger.info("GitHub: Authenticating")
    _auth = github.Auth.Token(token=token)
    connection = Github(auth=_auth)
    logger.info("GitHub: Fetching Repo")
    repo = connection.get_repo(repository)
    logger.info("GitHub: Fetching Branch")
    branch = repo.get_branch(branch)
    logger.info("GitHub: Fetching Commit")
    commit = repo.get_commit(branch.commit.sha)

    @retry(stop=stop_after_delay(timeout), wait=wait_fixed(interval), retry=retry_if_exception_type(Enforcer))
    def poll_vault():
        failed_state = False
        logger.debug(poll_vault.retry.statistics)
        logger.info(
            f"{BColors.Separator}============================="
            f"Attempt:{poll_vault.retry.statistics.get('attempt_number', 'Unknown')}"
            f"============================={BColors.ResetAll}"
        )
        logger.info("GitHub: Fetching Check Runs")
        check_runs = list(commit.get_check_runs())
        logger.debug("Logging All Checks")
        for run in check_runs:
            logger.debug(f"{BColors.Debug}{run.name} is {run.status} with conclusion {run.conclusion}")

        latest_runs = reduce_to_latest_runs(check_runs)

        _runs = list(filter(lambda r: r.name not in user_ignore, latest_runs))
        incomplete = filter_and_log_by_status(
            runs=_runs, statuses=Status.incomplete(), color=BColors.Teal, verb="Waiting on completion", marker=""
        )
        # incomplete = list(filter(lambda _run: _run.status in Status.incomplete(), _runs))
        # logger.info(f"{BColors.Teal}Waiting on completion of {len(incomplete)} workflows{BColors.ResetAll}")
        # for run in incomplete:
        #     logger.info(f"{run.name}")

        complete = list(filter(lambda _run: _run.status in Status.complete(), _runs))
        logger.info(f"Total completion of {len(complete)} workflows")

        filter_and_log_by_conclusion(
            complete=complete,
            conclusions=Conclusion.ignored(),
            color=BColors.Grey,
            verb="Ignored completion",
            marker="-",
        )
        filter_and_log_by_conclusion(
            complete=complete,
            conclusions=Conclusion.succeeded(),
            color=BColors.Green,
            verb="Celebrated completion",
            marker="✓",
        )
        failed_runs = filter_and_log_by_conclusion(
            complete=complete, conclusions=Conclusion.fail(), color=BColors.Red, verb="Mourned failure", marker="X"
        )

        if failed_runs:
            failed_state = True
            logger.fatal(f"{BColors.Red}Failed Executions {[fr.name for fr in failed_runs]}{BColors.ResetAll}")
            if not exhaustive:
                log_summary(latest_runs)
                raise Exception(f"{BColors.Red}Failed Executions Detected, bailing out{BColors.ResetAll}")
        if incomplete:
            logger.info(
                f"{BColors.Separator}========================"
                f"Retry in:{interval} seconds"
                f"========================{BColors.ResetAll}"
            )
            raise Enforcer("Still waiting on more workflows to complete")
        if failed_state:
            log_summary(latest_runs)
            raise Exception(f"{BColors.Red}Failed Executions Detected, bailing out{BColors.ResetAll}")
        return _runs

    try:
        poll_vault()
    except (Exception, RetryError):
        if debug:
            logger.exception("::error title=PR Checks Failed::Failed due to previously reported error. Review logs")
        else:
            logger.fatal("::error title=PR Checks Failed::Failed due to previously reported error. Review logs")
        sys.exit(1)


if __name__ == "__main__":
    hello()
