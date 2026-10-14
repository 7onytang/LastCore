# Frozen synthetic wire fixture

`linear-request-63d409a8.json` records the old linear request field set used by
the published 63d409a8 client cache binding. Its UUID and values are synthetic,
with no credentials or real task identity. The Controller test verifies exact
legacy serialization; the isolated node test hashes the same request and proves
an existing cached result is reused while the calculation function is forbidden.
No network, native client or real compute execution is needed.
