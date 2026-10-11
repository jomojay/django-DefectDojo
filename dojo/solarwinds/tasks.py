"""
Celery entry points for the SolarWinds Service Desk integration (INTEGRATIONS_ROADMAP.md 8.5/8.6).

Deliberately thin. Every task here resolves its arguments from ids, delegates to
:mod:`dojo.solarwinds.helper` and then decides what to do with the failure. Keeping
the logic out of the task bodies means the reconciliation and push paths are testable
by calling a plain function, with no eager-Celery scaffolding in the way.

**Retry policy.** ``client.py`` already retries 429 and transient 5xx inside the
request, honouring ``Retry-After`` and otherwise backing off exponentially with full
jitter for up to four attempts (capped at 60s). So a failure reaching this layer has
already survived that budget, and these tasks only make one further distinction:

* ``SolarWindsRateLimitError`` - the tenant is rate limiting for longer than an
  in-request budget can absorb. Retried by Celery on a minutes-scale countdown.
* auth / configuration / invalid-priority errors - a human has to change something.
  Retrying only burns rate-limit budget against a call that cannot succeed, so these
  are alerted once and dropped. This is R22's residual requirement: a misconfigured
  instance must not be mistaken for a transient failure.
* anything else (including a non-2xx that is not a 429) - alerted and dropped, for
  the same reason.

Nothing here raises into a finding-save request path: the push is dispatched
asynchronously, and even in the synchronous fallback (a user with ``block_execution``,
or an eager test run) the task catches its own errors and returns a value.

These tasks are registered through ``CELERY_IMPORTS`` in ``settings.dist.py``, because
``app.autodiscover_tasks`` only looks at ``<installed_app>/tasks.py`` and
``dojo.solarwinds`` is a package inside the ``dojo`` app, not an app of its own.
"""
import logging

from dojo.celery import app
from dojo.models import Finding
from dojo.solarwinds import helper
from dojo.solarwinds.client import SolarWindsError
from dojo.solarwinds.models import SolarWinds_Instance
from dojo.utils import get_object_or_none

logger = logging.getLogger(__name__)

# How long to wait before re-attempting a push the tenant rate limited. Minutes, not
# seconds: the client already exhausted the sub-minute window, so a short countdown
# would just re-collide with the same limit (R7).
RATE_LIMIT_RETRY_COUNTDOWN = 10 * 60
RATE_LIMIT_MAX_RETRIES = 3


def _can_retry(task):
    """
    Return whether ``task.retry()`` can actually reschedule anything.

    ``retry()`` on a directly-called or eagerly-applied task does not reschedule - it
    re-raises - so in those modes (tests, and users with ``block_execution`` set) the
    error is reported and dropped instead.
    """
    request = getattr(task, "request", None)
    if request is None:
        return False
    return not (getattr(request, "called_directly", True) or getattr(request, "is_eager", False))


@app.task(bind=True)
def push_finding_to_solarwinds(self, finding_id, *args, push_requested=False, **kwargs):
    """
    Create the SolarWinds incident for one finding.

    Dispatched from the finding-save path via
    :func:`dojo.solarwinds.helper.push_finding`. Returns the new ticket id, or ``None``
    when the push was skipped or failed.
    """
    finding = get_object_or_none(Finding, id=finding_id)
    if not finding:
        logger.warning(
            "Finding with id %s does not exist, skipping push_finding_to_solarwinds", finding_id,
        )
        return None

    try:
        ticket = helper.push_finding_internal(finding, push_requested=push_requested)
    except helper.TRANSIENT_ERRORS as exc:
        if _can_retry(self):
            logger.warning(
                "SolarWinds rate limited the push for finding %s; retrying in %ss: %s",
                finding_id, RATE_LIMIT_RETRY_COUNTDOWN, exc,
            )
            raise self.retry(
                exc=exc,
                countdown=RATE_LIMIT_RETRY_COUNTDOWN,
                max_retries=RATE_LIMIT_MAX_RETRIES,
            ) from exc
        helper.log_solarwinds_alert(
            f"SolarWinds push rate limited for finding {finding_id}",
            f"Finding {finding_id} could not be pushed because the tenant is rate "
            f"limiting and this run had no retry path available: {exc}",
            obj=finding,
        )
        return None
    except helper.PERMANENT_ERRORS as exc:
        # Not retried on purpose - see the module docstring.
        helper.log_solarwinds_alert(
            f"SolarWinds push failed for finding {finding_id}",
            f"Finding {finding_id} was not pushed and will not be retried automatically, "
            f"because this failure needs a configuration or credential change: {exc}",
            obj=finding,
        )
        return None
    except SolarWindsError as exc:
        helper.log_solarwinds_alert(
            f"SolarWinds push failed for finding {finding_id}",
            f"Finding {finding_id} could not be pushed to SolarWinds Service Desk: {exc}",
            obj=finding,
        )
        return None

    return ticket.id if ticket else None


@app.task
def poll_resolved_incidents(*args, **kwargs):
    """
    Reconcile resolved incidents back into findings, for every poll-enabled instance.

    Runs on Celery beat (see ``CELERY_BEAT_SCHEDULE``). One instance failing does not
    stop the others: a tenant with an expired token should not silently block
    reconciliation for a tenant whose token is fine.

    Returns ``{instance_id: ReconcileSummary}`` for the instances that completed.
    """
    summaries = {}
    for instance in SolarWinds_Instance.objects.filter(poll_enabled=True):
        try:
            summaries[instance.id] = helper.reconcile_resolved_incidents(instance)
        except helper.PERMANENT_ERRORS as exc:
            helper.log_solarwinds_alert(
                f"SolarWinds poll failed for '{instance}'",
                f"Resolved-incident reconciliation for '{instance}' could not run and needs "
                f"a configuration or credential change: {exc}",
            )
        except SolarWindsError as exc:
            # Includes rate limits: the next scheduled run is the retry, and the
            # reconciliation is idempotent (R20), so nothing is lost by waiting.
            helper.log_solarwinds_alert(
                f"SolarWinds poll failed for '{instance}'",
                f"Resolved-incident reconciliation for '{instance}' failed: {exc}. The next "
                f"scheduled run will retry; reconciliation is idempotent so no state is lost.",
            )
        except Exception:
            # A bug in reconciliation for one tenant must not silently take the whole
            # beat task down for the others.
            logger.exception(
                "SolarWinds: unexpected error reconciling instance '%s'", instance,
            )
    return summaries


@app.task
def check_instance_tokens(*args, **kwargs):
    """
    Probe every sync-enabled instance's credentials and alert on rejection (R6).

    Separate from :func:`poll_resolved_incidents` because it has to cover
    **push-only** instances too, which the poll never touches - and a push-only
    tenant is precisely the configuration where a rotated token stays invisible
    longest, since nothing runs on a schedule to notice.

    Returns ``{instance_id: bool}``.
    """
    results = {}
    for instance in helper.instances_needing_health_check():
        try:
            results[instance.id] = helper.verify_instance_token(instance)
        except Exception:
            logger.exception(
                "SolarWinds: unexpected error health-checking instance '%s'", instance,
            )
            results[instance.id] = False
    return results
