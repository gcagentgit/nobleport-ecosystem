# NoblePort.eth — Decentralized ENS Gateway

**Status tier:** Simulation-Validated (gateway code is built and unit-tested; on-chain records for subnames are not yet set — see "What is actually on-chain").
**Governance:** all record writes are L3/L4 Safe transactions. The gateway is read-only by construction.

## What it is

`nobleport.eth` is the control plane of a *federated* gateway. Every NoblePort product is a subname; the parent name carries the shared identity (addresses, contenthash, text records) and each subname carries its own. A resolver service in `nobleport-gateway` reads the namespace live from Ethereum through a pool of public RPC endpoints and routes visitors to content through several independent IPFS/ENS gateways. No single RPC provider, pinning service, or gateway operator can take the front door down.

```
                       ┌──────────────────────────────────────┐
  browser / agent ───▶ │  nobleport-gateway  (Express + tRPC) │
                       │  /gateway  /gw/:name  /api/ens/*     │
                       └───────┬───────────────────┬──────────┘
                               │ read-only         │ HEAD probes
                     ┌─────────▼─────────┐   ┌─────▼──────────────────┐
                     │ RPC pool (viem    │   │ IPFS / ENS gateways    │
                     │ fallback, ranked) │   │ dweb.link · ipfs.io ·  │
                     │ publicnode ·      │   │ w3s.link · pinata ·    │
                     │ cloudflare · llama│   │ 4everland · eth.limo   │
                     │ drpc · 1rpc · fb  │   └────────────────────────┘
                     └─────────┬─────────┘
                               ▼
                   ENS Registry + Universal Resolver (mainnet)
                   nobleport.eth ─┬─ stephanie · gcagent · permitstream
                                  ├─ cyborg · verify
                                  ├─ pay (SOL/USDC) · token · treasury · escrow
                                  └─ docs · dao
```

## Layers (maps to the framework in the build brief)

| Layer | Where | What it does |
|---|---|---|
| Driver / adapter | `server/ens/client.ts` | viem `PublicClient` over a `fallback` transport; latency-ranked, keyless, read-only |
| Data processing | `server/ens/encoding.ts`, `contenthash.ts` | base58/base32/base36/varint; EIP-1577 contenthash decode/encode (ipfs, ipns, swarm, arweave) |
| Blockchain interface | `server/ens/resolver.ts` | ENSIP-15 normalize, namehash, `addr(60/501)`, `contenthash`, ENSIP-5 texts, reverse-record check; 60 s cache |
| Security | `server/ens/siwe.ts` | Sign-In With Ethereum (EIP-4361): origin-bound single-use nonces, offline ecrecover, app session cookie |
| Coordination / control | `server/ens/manifest.ts`, `drift.ts` | declared namespace + evidence tiers + governance levels; on-chain vs declared diff; prepared resolver calldata + Safe multicall |
| Gateway routing | `server/ens/gateways.ts`, `routes.ts` | content-addressed URLs first, then eth.limo, then the `url` text record; `/gw/:name` 302s to the best one |

## Surfaces

| Surface | Purpose |
|---|---|
| `GET /gateway` (page) | Explorer: resolve any name, subname directory with tiers, RPC/IPFS health, admin drift console |
| `GET /gw/:name` | Redirect to the best gateway for the name (`?format=json` to inspect instead) |
| `GET /api/ens/resolve/:name` | JSON profile + ordered gateway targets |
| `GET /api/ens/directory` | Root + all manifest subnames, resolved concurrently |
| `GET /api/ens/health?probe=1` | RPC pool liveness (503 when every endpoint fails) |
| `GET /.well-known/nobleport-ens.json` | The manifest, for discovery |
| tRPC `ens.*`, `wallet.nonce`, `wallet.verify` | Same data for the React app; `ens.drift` is admin-only |

## The manifest is declared state, not live state

`scripts/ens-records.json` (this repo) and `server/ens/manifest.ts` (gateway repo) declare what each name *should* hold and the evidence tier each product is honestly at. The gateway shows on-chain truth next to it and reports drift. If the chain cannot be read, the gateway says so (HTTP 5xx) rather than reporting "not on-chain".

Evidence tiers: `Deployed` · `In-Audit` · `Simulation-Validated` · `Roadmap`. No subname is marked Deployed today.

## What is actually on-chain (as of this commit)

- `nobleport.eth` is registered and owned (see `SOLANA_KEY_VERIFICATION.md`, `scripts/verify-ens-solana.js`).
- Subnames (`stephanie.nobleport.eth`, `pay.nobleport.eth`, …) have **not** been created. The directory will show them as "not on-chain" until they are.
- The root `contenthash` is intentionally unset until a build is pinned and approved (L4).

This was not verifiable from the build sandbox (egress to public RPCs is blocked there). Verify with:

```bash
export ETH_RPC_URL="https://ethereum-rpc.publicnode.com"
node scripts/verify-ens-solana.js
node scripts/ens-drift-check.js            # prints drift + prepared calldata; sends nothing
```

## Changing records (human-gated)

1. Edit `scripts/ens-records.json` and the mirrored `manifest.ts`; open a PR.
2. Run `node scripts/ens-drift-check.js` (or the admin drift console at `/gateway`). It prints one `multicall(bytes[])` per name for the PublicResolver.
3. Write the AuditBeacon entry.
4. Submit the multicall through the Safe at the printed level: **L3** (text records, 2 signers) or **L4** (addresses, contenthash, 3-of-5).
5. Re-run the drift check; it must report `IN SYNC`.

To create a subname the owner of `nobleport.eth` calls `setSubnodeRecord` on the ENS registry (or NameWrapper) via the same Safe flow, pointing it at the PublicResolver. The gateway does not create subnames.

## Configuration (gateway repo)

| Variable | Default | Notes |
|---|---|---|
| `ETH_RPC_URLS` | six keyless public mainnet endpoints | comma-separated; first is preferred, ranking re-orders at runtime |
| `IPFS_GATEWAYS` | ipfs.io, dweb.link, w3s.link, pinata, 4everland, trustless-gateway.link | https only |
| `ENS_GATEWAY_HOSTS` | `eth.limo,eth.link` | ENS-aware HTTPS gateways |
| `JWT_SECRET` | — | required for wallet sign-in sessions (shared with OAuth sessions) |

## Boundaries (from the operating rules)

- The gateway holds no keys and exposes no write path. It prepares calldata only.
- USDC on the Solana rail (`pay.nobleport.eth`) is payments-only; NBPT is governance/utility. The manifest keeps them on separate subnames and the `pay` record carries no treasury, governance, NBPT, or bridge authority.
- Wallet sign-in proves ownership; it never authorizes a transaction. The SIWE statement says so in the message the user signs.
- No "live" claim without a receipt: the UI badges come from chain reads, and the manifest tiers are conservative.
