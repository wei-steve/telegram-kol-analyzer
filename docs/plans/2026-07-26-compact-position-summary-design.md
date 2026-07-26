# Compact Position Summary Design

The position-card summary is limited to `开仓均价` and `数量`. Verified stop and
take-profit information is already presented in the exact, ordered
`止盈止损(n)` list immediately below, so duplicating it in the compact grid adds
noise without adding a decision-useful view. The detailed list remains
unchanged, including trigger price, size, order ID, and attribution status.
