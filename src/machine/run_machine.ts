import { assign, setup } from 'xstate';

/**
 * otc run-loop state machine (XState v5) — the authoritative outline.
 *
 * Mirrors src/runner.py: one step walks
 *   seeing -> deciding -> gating -> [acting] -> verifying
 * and ends in done / stopped. Idle paths skip `acting`. Async work
 * (perceive/decide/propose/act) runs as invoked promise actors;
 * pure checks are named guards; context (not states) carries counters,
 * memory, and outcomes — per the xstate-v5 skill conventions.
 *
 * A stale/covered click detours acting -> healing -> acting (one
 * label-remapped re-attempt) or acting -> healing -> verifying.
 * The executable twin is src/machine/run_engine.py (python-statemachine),
 * which the runner drives event-by-event; this file is the readable spec.
 *
 * The parallel `session` region models the browser side: the active page
 * handle and the cursor-tracker lifecycle (tracking <-> navigating <->
 * destroyed -> recovering), which is where the tab-switch race lived
 * (stale tabs firing recovery on the wrong page — now generation-guarded).
 */

interface RunContext {
  steps: number;
  moves: number;
  noops: number;
  deadRun: number;
  visited: string[];
  notes: string[];
  stopReason: string | null;
  blocked: string | null;
  synthAttempted: boolean;
  readingUntilStep: number;
}

type RunEvent =
  | { type: 'STEP_TICK' }
  | { type: 'PERCEIVED'; blocked: string | null }
  | { type: 'DECIDED' }
  | { type: 'STEER_STOP' }
  | { type: 'GOTO_DENIED' }
  | { type: 'ACTED' }
  | { type: 'IDLED' }
  | { type: 'HEAL_NEEDED'; reason: string }
  | { type: 'HEALED'; item: number }
  | { type: 'HEAL_FAILED' }
  | { type: 'DONE_ACCEPTED' }
  | { type: 'LIMITS_HIT'; reason: string }
  | { type: 'SYNTH_DONE'; note: string }
  | { type: 'TAB_SWITCHED'; url: string };

export const otcRunMachine = setup({
  types: {
    context: {} as RunContext,
    events: {} as RunEvent,
  },
  actions: {
    bankNote: assign({
      // append extractive note + visited URL on first settled sighting
      notes: ({ context, event }) => context.notes,
      visited: ({ context }) => context.visited,
    }),
    countNoop: assign({ noops: ({ context }) => context.noops + 1 }),
    countDeadRun: assign({ deadRun: ({ context }) => context.deadRun + 1 }),
    resetDeadRun: assign({ deadRun: 0 }),
    recordStop: assign({
      stopReason: ({ event }) =>
        event.type === 'LIMITS_HIT' ? event.reason : 'steer stop',
    }),
    recordBlocked: assign({
      // blocked bot-check pages stay IN the loop: the flag rides in state
      // so Jev classifies positions on the challenge controls like any page.
      blocked: ({ event }) =>
        event.type === 'PERCEIVED' ? (event.blocked ?? null) : null,
    }),
    startReadingCooldown: assign({
      // readingUntilStep = steps + READING_COOLDOWN_STEPS on tab switch
      readingUntilStep: ({ context }) => context.readingUntilStep,
    }),
  },
  guards: {
    isBlocked: ({ event }) =>
      event.type === 'PERCEIVED' && event.blocked !== null,
    confidenceOk: ({ event }) =>
      // kind Choice confidence >= min_confidence (DONE exempt)
      event.type === 'DECIDED',
    doneAccepted: () => false, // kind==done && settled && task_done>=0.5 && note quotable
    shouldAct: () => true, // an acting branch ran (click/type/enter/goto/...)
    limitsHit: ({ context }) =>
      context.noops >= 3 || context.deadRun >= 2,
    gotoAllowed: ({ context }) => true, // steps > readingUntilStep
  },
}).createMachine({
  id: 'otc-run',
  initial: 'seeing',
  context: {
    steps: 0,
    moves: 0,
    noops: 0,
    deadRun: 0,
    visited: [],
    notes: [],
    stopReason: null,
    blocked: null,
    synthAttempted: false,
    readingUntilStep: 0,
  },
  states: {
    seeing: {
      // perceive: screenshot, element map, focused field, page text, tabs.
      // First settled sighting banks a note (bankNote). A blocked
      // (bot-check) page records context and keeps going — Jev classifies
      // its controls like any page; there is no blocked terminal.
      on: {
        PERCEIVED: { target: 'deciding', actions: ['bankNote', 'recordBlocked'] },
        STEER_STOP: { target: 'stopped', actions: 'recordStop' },
      },
    },
    deciding: {
      // invoke proposeAction + decideAction (promise actors); onDone records.
      invoke: {
        src: 'decideStep',
        onDone: { target: 'gating' },
        onError: { target: 'gating' },
      },
    },
    gating: {
      // confidence gate, loop-guard, Noul gates, proposal routing.
      always: [
        { target: 'done', guard: 'doneAccepted' },
        { target: 'acting', guard: 'shouldAct' },
        { target: 'verifying' },
      ],
    },
    acting: {
      // one deterministic handler (click/type/enter/goto/back/challenge/...);
      // click auto-adopts fresh tabs; failures report error: prefix.
      // A stale/covered click raises HEAL_NEEDED instead of dying here.
      invoke: {
        src: 'executeAction',
        onDone: { target: 'verifying' },
        onError: { target: 'verifying' },
      },
      on: {
        HEAL_NEEDED: { target: 'healing' },
      },
    },
    healing: {
      // self-healing retry: re-probe the page, remap the failed target by
      // label, re-attempt exactly once (HEALED carries the fresh item).
      // No remap target -> HEAL_FAILED accounts the failure in verifying.
      invoke: {
        src: 'healTarget',
        onDone: { target: 'acting' },
        onError: { target: 'verifying' },
      },
    },
    verifying: {
      // fingerprint vs last step (no-effect), stop-rule evaluation,
      // synthesis verdict as last resort before stopping.
      always: [
        { target: 'stopped', guard: 'limitsHit' },
        { target: 'seeing' },
      ],
    },
    done: { type: 'final' },
    stopped: { type: 'final' },
  },
});
