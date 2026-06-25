"""Optional: auto-run the pipeline whenever a *new* RxNormConcept is added.

This is opt-in. Enable it by setting ``RXGRAPH_AUTO_MATERIALIZE = True`` in
Django settings AND connecting the receiver from your app's ``AppConfig.ready``
(see README). The recommended path for most callers is the explicit
``rxgraph.pipeline.add_concept(rxcui)`` -- a signal that fires network calls on
every save is convenient but easy to trigger by accident.

Why the guard: materializing a concept creates *more* RxNormConcept rows
(its IN/PIN/SCDC/...). Without protection, each of those creations would
re-enter the pipeline. ``_GUARD`` is a thread-local flag held for the duration
of a materialize run, so child creations during that run are ignored.
"""

from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .pipeline import _resolve_models, materialize_concept

log = logging.getLogger(__name__)
_GUARD = threading.local()


def _auto_enabled() -> bool:
    return bool(getattr(settings, "RXGRAPH_AUTO_MATERIALIZE", False))


def connect() -> None:
    """Wire the post_save receiver. Call from ``AppConfig.ready()``."""
    concept_model, _ = _resolve_models()
    post_save.connect(
        _on_concept_created,
        sender=concept_model,
        dispatch_uid="rxgraph.auto_materialize",
    )


@receiver(post_save)  # sender bound in connect(); decorator is a no-op fallback
def _on_concept_created(sender, instance, created, **kwargs):
    if not created or not _auto_enabled():
        return
    if getattr(_GUARD, "active", False):
        return  # we are already inside a materialize run
    rxcui = getattr(instance, "rxcui", None)
    if not rxcui:
        return

    def _run():
        _GUARD.active = True
        try:
            materialize_concept(rxcui)
        except Exception:  # noqa: BLE001 - never break the user's save
            log.exception("rxgraph auto-materialize failed for rxcui=%s", rxcui)
        finally:
            _GUARD.active = False

    # Defer until after the triggering transaction commits, so we never fetch
    # over the network while that transaction is still open.
    transaction.on_commit(_run)
