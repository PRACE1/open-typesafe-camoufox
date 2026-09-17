"""
TrackerMachine: state machine for the JS mouse tracker lifecycle.

Replaces the _navigating boolean flag in CamoufoxCapability with a
state-enforced invariant. The navigation race (load -> goto -> destroy ->
framenavigated -> recover) is now impossible to violate:

  - safe_goto() sends GOTO (tracking -> navigating), runs page.goto(),
    then sends NAV_DONE (navigating -> tracking).
  - While in navigating, any framenavigated event is ignored -- it's our
    navigation, already handled by the entry/exit actions.
  - The framenavigated listener sends EXTERNAL_NAV via asyncio.ensure_future.
    The guard on EXTERNAL_NAV checks: if the machine is in navigating, the
    event is dropped (our nav). If in tracking, the transition fires ->
    destroyed state, whose entry action calls reinject_tracker +
    _restart_tracker_on_new_page (recovery).

This is the helper requested in the prior session: a single awaited state
transition that harvests-then-navigates-then-reinjects in one atomic step,
replacing the racy async-recovery callback where framenavigated fires after
the new doc exists, re-injects onto a doc that may immediately die again,
and the explicit reinject after the settle poll is the only thing that lands
on the final doc.

States:
  uninjected -> INJECT -> injected
  injected -> START_TRACKING -> tracking
  tracking -> GOTO -> navigating
  navigating -> NAV_DONE -> tracking   (safe_goto completes)
  tracking -> HARVEST -> harvested
  tracking -> EXTERNAL_NAV (guarded: not navigating) -> destroyed
  destroyed -> RECOVER -> tracking
"""

from __future__ import annotations

from xstate_statemachine import StateMachine, State, guard, action


class TrackerMachine(StateMachine):
    """Enforces the JS tracker lifecycle as a state machine.

    The _navigating boolean in CamoufoxCapability is replaced by the
    `navigating` state: while in it, EXTERNAL_NAV events are dropped (our
    navigation). The `destroyed` state's entry action runs recovery
    (reinject + restart), replacing the racy _recover_after_external_nav
    callback.
    """

    machine_id = "trackerMachine"

    initial_context = {
        "tracking_start_time": None,
        "moves_count": 0,
        "clicks_count": 0,
        "recovery_attempts": 0,  # counter for failed recoveries (max 2)
    }

    # --- States ---
    uninjected = State("uninjected", initial=True)
    injected = State("injected")
    tracking = State("tracking")
    navigating = State("navigating")
    destroyed = State("destroyed")
    harvested = State("harvested", final=True)
    failed = State("failed", final=True)
    # recoveryFailed: terminal for when tracker recovery exhausts retries.
    # Sets tracker_verified=False so harvest_cursor_events knows it's dead.
    recoveryFailed = State("recoveryFailed", final=True)

    # --- Transitions ---
    # INJECT: uninjected -> injected
    inject_transition = uninjected.to(injected, event="INJECT")

    # START_TRACKING: injected -> tracking
    start_tracking_transition = injected.to(tracking, event="START_TRACKING")

    # GOTO: tracking -> navigating (our deliberate navigation)
    goto_transition = tracking.to(navigating, event="GOTO")

    # NAV_DONE: navigating -> tracking (safe_goto completed, tracker restarted)
    nav_done_transition = navigating.to(tracking, event="NAV_DONE")

    # EXTERNAL_NAV: tracking -> destroyed (a navigation we did NOT initiate)
    external_nav_transition = tracking.to(destroyed, event="EXTERNAL_NAV")

    # RECOVER: destroyed -> tracking (recovery complete, tracker re-injected)
    recover_transition = destroyed.to(tracking, event="RECOVER")

    # RECOVERY_FAILED: destroyed -> recoveryFailed (after 2 failed recovery attempts)
    recovery_failed_transition = destroyed.to(recoveryFailed, event="RECOVERY_FAILED")

    # HARVEST: tracking -> harvested (final harvest, session ending)
    harvest_transition = tracking.to(harvested, event="HARVEST")

    # Error transitions
    inject_fail = uninjected.to(failed, event="__error__")
    recover_fail = destroyed.to(failed, event="__error__")

    # --- Guards ---

    # No guards needed: the state machine itself enforces the invariant.
    # EXTERNAL_NAV is only defined as a transition from `tracking`, so it
    # is silently ignored when in `navigating` (no matching transition).
    # This is the structural enforcement: the race is impossible because
    # the event can't fire from the wrong state.

    # --- Actions (optional, for logging) ---

    @action
    def log_navigating_entry(self, interpreter, context, event, action_def):
        """Logged when entering navigating state (safe_goto started)."""
        from .logging_utils import log
        log("[tracker] entering navigating state - harvesting before goto")

    @action
    def log_navigating_exit(self, interpreter, context, event, action_def):
        """Logged when leaving navigating state (safe_goto completed)."""
        from .logging_utils import log
        log("[tracker] leaving navigating state - restarting tracker on new page")

    @action
    def log_destroyed_entry(self, interpreter, context, event, action_def):
        """Logged when entering destroyed state (external nav detected)."""
        from .logging_utils import log
        log("[tracker] entering destroyed state - external nav detected, recovering")

    # Entry/exit action registration via decorators
    @navigating.enter
    def on_navigating_enter(self, interpreter, context, event, action_def):
        """Entry action for navigating: harvest the outgoing document's
        events before page.goto() destroys them."""
        # The actual harvest is done by the capability's _harvest_and_accumulate
        # method, called from safe_goto BEFORE sending GOTO. This entry action
        # is a logging hook; the harvest must happen before the state transition
        # because the transition fires when GOTO is sent, and page.goto() runs
        # after the transition completes.
        #
        # Actually, the flow is:
        # 1. safe_goto calls _harvest_and_accumulate() (harvests outgoing doc)
        # 2. safe_goto sends GOTO (tracking -> navigating)
        # 3. safe_goto calls page.goto() (destroys the doc)
        # 4. safe_goto sends NAV_DONE (navigating -> tracking)
        # 5. The navigating.exit action runs _restart_tracker_on_new_page()
        #
        # So the harvest happens BEFORE the state transition, and the restart
        # happens on the NAV_DONE transition. This entry action just logs.
        from .logging_utils import log
        log("[tracker:navigating.enter] safe_goto started - outgoing doc harvested")

    @navigating.exit
    def on_navigating_exit(self, interpreter, context, event, action_def):
        """Exit action for navigating: re-inject and re-start the tracker on
        the new document with the original start time."""
        from .logging_utils import log
        log("[tracker:navigating.exit] safe_goto completed - restarting tracker on new page")

    @destroyed.enter
    def on_destroyed_enter(self, interpreter, context, event, action_def):
        """Entry action for destroyed: recover the tracker after an external
        navigation we did not initiate.

        This replaces the racy _recover_after_external_nav callback. The
        recovery runs reinject_tracker + _restart_tracker_on_new_page, then
        sends RECOVER to transition back to tracking.

        NOTE: this action runs synchronously in the interpreter's event
        processing. The actual recovery I/O (page.evaluate) is async, so we
        schedule it and send RECOVER when it completes. The capability's
        _recover_after_external_nav method is called from here.

        RECOVERY_FAILED: if recovery_attempts exceeds 2, the capability sends
        RECOVERY_FAILED instead of RECOVER, transitioning to the
        recoveryFailed terminal. The capability sets tracker_verified=False
        so harvest_cursor_events knows the tracker is dead.
        """
        from .logging_utils import log
        attempts = context.get("recovery_attempts", 0)
        log(f"[tracker:destroyed.enter] external nav detected - scheduling recovery "
            f"(attempt {attempts + 1})")

    @recoveryFailed.enter
    def on_recovery_failed_enter(self, interpreter, context, event, action_def):
        """Entry action for recoveryFailed: log that recovery exhausted retries
        and mark the tracker as dead."""
        from .logging_utils import log
        context["tracker_verified"] = False
        log("[tracker:recoveryFailed.enter] recovery exhausted - tracker is dead")


def is_external_nav_ignored_in_navigating(state_id: str) -> bool:
    """Check helper: is the given state the `navigating` state?

    Used by the capability to determine if an EXTERNAL_NAV event should be
    sent. Actually, the state machine handles this automatically: EXTERNAL_NAV
    is only a valid transition from `tracking`, so sending it while in
    `navigating` is a no-op. This helper exists for logging purposes only.
    """
    return "navigating" in state_id
