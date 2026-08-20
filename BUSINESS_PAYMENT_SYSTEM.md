# TASKORA WORK — Advertiser Payment System

## Recommended production flow

1. Advertiser creates a campaign.
2. Advertiser enters:
   - Reward per completed worker.
   - Number of workers required.
   - Task details, link and deadline.
3. TASKORA calculates:
   - Worker budget = reward × workers.
   - Platform fee = 20% of worker budget by default.
   - Campaign total = worker budget + platform fee.
4. Advertiser pays the campaign total through Flutterwave (Card, Bank Transfer or USSD), or uses an existing advertiser wallet balance.
5. The server verifies the Flutterwave transaction before crediting the advertiser wallet.
6. The campaign total is reserved against that campaign.
7. Admin sees the funded campaign and can Approve & Publish or Reject.
8. Approved campaigns appear to activated Workers.
9. Worker submits proof.
10. Admin approves/rejects the proof. Advertiser cannot release worker money directly.
11. On approval, the system settles exactly one worker reward plus the proportional platform fee from the campaign reserve.
12. The platform fee is recorded separately in `platform_revenue` for Admin reporting.
13. When all slots are completed, the campaign becomes `completed`.
14. If a campaign expires, is rejected, or is closed before all slots are completed, unused reserved funds are returned to the advertiser wallet. Pending worker proofs are closed when a campaign is rejected/closed by Admin.

## Why this model is safer

- Advertisers fund before a campaign can be published.
- Workers are not paid from money that has not been reserved.
- Admin controls both campaign approval and final worker-proof approval.
- Duplicate Flutterwave callbacks are protected by unique payment event references.
- Unused campaign money is not treated as platform revenue.
- The platform fee is earned only when a worker submission is approved.
- The advertiser can see campaign status, progress, worker submissions and wallet transactions without being able to approve worker earnings.

## Example

If a campaign pays ₦150 per worker and needs 20 workers:

- Worker budget: ₦3,000
- 20% platform fee: ₦600
- Amount to fund: ₦3,600

If only 12 workers are approved:

- Worker rewards settled: ₦1,800
- Platform fees earned: ₦360
- Unused reserve returned: ₦1,440

The exact amounts are calculated server-side.
