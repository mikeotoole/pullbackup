// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole

/**
 * "This task's schedule stopped firing."
 *
 * INCIDENT 2026-09-06..10: every row on the task list read `success` for four
 * days while nothing executed. That was the last COMPLETED run's state, which
 * is a different fact from whether the task is still running, and the list
 * could only show the first one. This flag is the second.
 *
 * Deliberately NOT a sixth `StatusPill` state. The pill describes the last
 * run; folding "missed" into its vocabulary would mean a row could report the
 * run OR the schedule but never both — and "last run succeeded, and nothing
 * has fired since" is exactly the pair of facts that went unnoticed.
 *
 * Carries a word and an accessible name, not just a colour: the incident was
 * noticed by nobody looking at this screen, so the flag has to survive being
 * read quickly, at a glance, by someone who is not colour-attentive.
 */
import { WarningIcon } from "./icons";

export function MissedScheduleFlag({ missed }: { missed?: boolean }) {
  // Undefined, not just false: an older server, or a response shaped before
  // this field existed, must degrade to the previous behaviour rather than
  // flag every row in the list.
  if (!missed) return null;
  return (
    <span
      data-missed-schedule="true"
      aria-label="missed schedule"
      title="this task's next run is overdue and nothing is executing — the scheduler may have stopped firing it"
      className="inline-flex items-center gap-1 border border-danger text-danger rounded-full px-2 py-0.5 text-xs whitespace-nowrap"
    >
      <WarningIcon className="w-3.5 h-3.5" />
      missed schedule
    </span>
  );
}
