---
title: Lost packages and stalled tracking
category: shipping
---
## When a package counts as lost
We treat a package as lost when the carrier reports it lost, or when it is in transit and the tracking hasn't been updated for {{refunds.lost_package_days_without_scan}} days or more.

## What we refund
For a lost package we refund the items that haven't already been refunded, plus the shipping fee, to the original payment method. Refunds over {{refunds.auto_approve_limit_cents | money}} are reviewed by our team first, usually {{approvals.expected_decision}}.

## If tracking is only a few days old
Carriers sometimes skip scans between facilities. If the last update is more recent than {{refunds.lost_package_days_without_scan}} days, please give it a little more time.

## If the package turns up later
If a refunded package arrives after all, please contact us so we can arrange a return label.
