# TASKORA WORK — Business / Advertiser System Policy

- Advertiser funding provider: Flutterwave.
- Advertiser campaign platform fee: 20% by default (`ADVERTISER_PLATFORM_FEE_PERCENT`).
- Advertiser sets the reward paid to one worker and the number of workers required.
- Campaign total = worker rewards + platform fee.
- A campaign must be fully funded before Admin can publish it to Workers.
- Advertiser money is kept in an internal campaign wallet/reserve after successful server-side payment verification.
- Admin approves/rejects advertiser campaigns before publication.
- Worker submissions are approved/rejected by TASKORA Admin only.
- Advertisers can view their own submissions, campaign progress and analytics but cannot release worker rewards.
- When Admin approves a worker submission, the worker reward and proportional platform fee are settled from the campaign reserve.
- Platform fee is recorded separately in `platform_revenue` and is not earned on unused campaign budget.
- If a campaign is rejected, closed or expires with unused reserved funds, those unused funds return to the advertiser wallet.
- If a campaign is closed while worker proofs are pending, those pending proofs are closed so refunded funds cannot later be spent twice.
- Advertiser data is scoped by advertiser ownership; no cross-business task/submission access.
- Flutterwave transaction references and payment event records are idempotent to prevent duplicate wallet credits.
- Worker activation remains separate from advertiser funding. Worker activation is still ₦3,000 and becomes permanent after verified payment.
- Worker minimum withdrawal remains ₦2,000 and referral reward remains ₦500 according to the current TASKORA configuration.
