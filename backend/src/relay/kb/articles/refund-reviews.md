---
title: Why some refunds are reviewed by our team
category: refunds
---
Most refunds are issued as soon as you confirm them. A few are reviewed by a person first.

## When a refund is reviewed
- The refund is larger than {{refunds.auto_approve_limit_cents | money}}.
- The account has had {{refunds.velocity_refunds_before_review}} or more refunds through our support assistant in the last {{refunds.velocity_window_days}} days.

## How long it takes
Reviews usually finish {{approvals.expected_decision}}. You don't need to stay in the chat: the decision appears in your conversation, and you can keep asking other questions meanwhile.

## If the request isn't decided in time
If a request isn't decided within {{approvals.expire_after_hours}} hours, a team member takes over your case personally.
