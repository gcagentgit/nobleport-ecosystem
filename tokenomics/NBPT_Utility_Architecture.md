# NBPT Utility Architecture
## How the token would do work inside the construction pipeline

**NoblePort Systems | ERC-3643 Security Token | Mainnet Pending**

> **IMPORTANT NOTICE:** This document describes a proposed technical
> framework. It is not an offering, solicitation, or investment
> recommendation. No tokens are available. Any future offering will be
> conducted under applicable securities exemptions with counsel-reviewed
> documentation. Nothing here is a projection of value, income, or return.
> Public materials must not use the words on the `danger_words` list in
> `core/config/launch-gates.json`; this document is written to that rule.

Companion to `NBPT_Ultra_Scarce_Model.md` (supply) and
`contracts/src/property/PropertyRegistry.sol` (fractional property
interests). Status tier for everything below: **Roadmap** unless marked.

---

## Operating rules this design must respect

| Rule | Source | Consequence for NBPT |
|---|---|---|
| USDC is for payments; NBPT is for governance and utility; never mix | Stephanie.ai constitution | Customers, subcontractors and SPV distributions settle in USDC or fiat. NBPT never prices construction work. |
| L3 = dual approval, L4 = 3-of-5; audit entry before any state change | Stephanie.ai constitution | Every mechanism below ends in a human-signed transaction with an AuditBeacon record first. |
| No "live" without a receipt | Evidence standard | Every mechanism is labelled with its tier. None is Deployed. |
| `nbpt_sale`, `tokenized_fractional_ownership`, `yield_staking_dashboards`, `public_secondary_market` are BLOCKED | launch-gates (red) | No mechanism below creates a sale, a secondary market, or a return. |

---

## Mechanism 1 — Network fee medium

**What it is.** Platform actions that write to the chain (a progress report
anchor, an inspection certificate, a registry attestation) cost gas. NoblePort
would sponsor that gas through a paymaster so field users never hold ETH,
and meter the sponsored usage in NBPT as a platform-access unit.

**How it would work.**

- Arbitrum One is the execution layer; gas is settled in ETH by the paymaster.
- Each sponsored operation debits a metered NBPT allowance held by the
  organisation (contractor, inspection firm, SPV), not by individuals.
- Allowances are granted and replenished by the operating entity under L3
  approval; they are consumed, not transferred, by field operations.
- NBPT never leaves the organisation's wallet to pay a person for work.

**Real-world application.** A superintendent logs a framing-complete report
from a phone. The report hash is anchored on Arbitrum through the paymaster;
the organisation's NBPT access allowance is debited one unit; the
superintendent never touches a wallet. An inspector submitting a compliance
certificate works the same way.

**Tier:** Roadmap. Depends on AnchorRegistry (staged) and a paymaster
integration that has not been built.

---

## Mechanism 2 — Performance bond escrow

**What it is.** A builder or subcontractor posts a performance bond in NBPT
against a project phase, held in an escrow contract. The bond is released on
passed inspection and forfeited, in whole or in part, on documented failure.
This is the on-chain form of the retainage and performance-bond practice the
construction company already uses; it is not a reward programme and it pays
nothing for holding.

**How it would work.**

- Bond size is a percentage of the phase contract value, set in the written
  scope (the `awo_tracking` lane, which is GO).
- Release and forfeiture are triggered only by a **composite attestation**:
  inspection result plus a second signer. The escrow contract exposes no
  autonomous slashing; forfeiture is an L3 action with an AuditBeacon entry.
- Forfeited bonds go to the SPV or project owner to fund remediation, not to
  a treasury and not to other token holders.
- Disputes follow the contract's written dispute process; the chain records
  the outcome, it does not adjudicate it.

**Real-world application.** A subcontractor bonds 5% of a $400,000 rough-in
phase. The municipal inspection fails on a documented deficiency; the
PermitStream review and a second NoblePort signer confirm. The portion of the
bond defined in the scope is forfeited to the project to pay for the fix; the
rest releases when the re-inspection passes.

**Tier:** Roadmap. Requires counsel review of bond treatment under
Massachusetts construction law and the money-transmission analysis already
flagged in launch-gates.

---

## Mechanism 3 — Governance consensus (PROPOSED)

**What it is.** NBPT holders would vote on framework questions that affect
the platform: which inspection firms are approved issuers in the credential
layer, which document standards a project must follow, and protocol-level
parameters such as bond percentages and anchor cadence.

**How it would work.**

- Off-chain Snapshot voting with proposals and results stored on IPFS
  (`dao.nobleport.eth`, marked PROPOSED in the ENS manifest).
- Votes are advisory inputs to the human governance process. Binding
  changes remain L3/L4 multi-sig actions; no vote executes code.
- Fractional property interests (PropertyRegistry balances) carry **no**
  governance weight. Property holders govern their SPV under its operating
  agreement, not the platform.
- Treasury allocation remains human multi-sig; governance can recommend,
  not spend.

**Tier:** Roadmap / PROPOSED. Not operating.

---

## What NBPT deliberately does not do

- It is not a payment for construction work (USDC/fiat).
- It is not a share of any property (PropertyRegistry fractions are separate
  instruments in separate SPVs).
- It does not accrue income to holders from bonds, fees, or anything else.
- It is not sold, listed, or made transferable on a public market under the
  current gates.

---

## Interfaces this architecture assumes

| Component | Location | Tier |
|---|---|---|
| NBPT security token (identity-gated ERC-20) | `contracts/src/token/NBPTSecurityToken.sol` | In-Audit |
| Identity registry + modular compliance | `contracts/src/identity`, `contracts/src/compliance` | In-Audit |
| Property registry (identity-gated ERC-1155 fractions) | `contracts/src/property/PropertyRegistry.sol` | Simulation-Validated (6 Hardhat tests) |
| Ledger sandbox (lifecycle simulation) | `dashboards/ledger-sandbox.html` | Simulation-Validated |
| Land program framework page | `dashboards/land-program.html` | Roadmap |
| Milestone anchors (AnchorRegistry) | Arbitrum One, staged | In-Audit |
| Paymaster / escrow / Snapshot space | not built | Roadmap |

Changes to any of the above that touch funds or public materials go through
the L3/L4 process described in `docs/ENS_GATEWAY.md` § Changing records.
