# TASKORA Business System Policy

- Advertiser funding provider: Flutterwave.
- Platform fee: 20%.
- Advertiser tasks require Admin approval before appearing to workers.
- Worker submissions are approved/rejected by Admin only.
- Advertisers may see status, progress and analytics for their own campaigns.
- Advertisers cannot approve submissions or release worker rewards.
- Campaign budgets must be reserved/verified server-side before a task becomes active.
- Advertiser data must be scoped by advertiser ownership; no cross-business task/submission access.
- Payment references must be idempotent so duplicate Flutterwave callbacks cannot double-credit a wallet.
