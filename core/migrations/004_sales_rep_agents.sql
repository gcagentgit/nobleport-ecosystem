-- NoblePort Sales Rep Agent System
-- Migration: 004_sales_rep_agents
--
-- Persistence for the "100 Salespeople" architecture (core/sales/). Keyed on
-- rep_id. HubSpot remains the CRM source of truth: these tables hold the
-- orchestrator's working state, the human-gate queue, and the rep learning
-- loop — every row that touches a customer references its HubSpot ids.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Reps ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_reps (
    rep_id            TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    email             TEXT,
    hubspot_owner_id  TEXT,
    territories       TEXT[] NOT NULL DEFAULT '{}',
    project_types     TEXT[] NOT NULL DEFAULT '{}',
    capacity          INT NOT NULL DEFAULT 25,
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Opportunities --------------------------------------------------------------
-- Every opportunity carries the management-required fields as columns.
CREATE TABLE IF NOT EXISTS sales_opportunities (
    opportunity_id    TEXT PRIMARY KEY,
    contact_id        TEXT,                          -- HubSpot contact id
    deal_id           TEXT,                          -- HubSpot deal id
    property_address  TEXT,
    source            TEXT NOT NULL DEFAULT 'other'
        CHECK (source IN ('social','phone','website','email','referral','other')),
    channel           TEXT,
    project_type      TEXT NOT NULL DEFAULT 'other',
    pipeline_stage    TEXT NOT NULL DEFAULT 'lead_entered'
        CHECK (pipeline_stage IN ('lead_entered','deduped','qualified','assigned','responded',
                                  'appointment','scope_estimate','follow_up','contract_deposit',
                                  'production_handoff','closed_won','closed_lost')),
    last_touch        TIMESTAMPTZ,
    next_action       TEXT,
    next_action_due   TIMESTAMPTZ,
    assigned_rep      TEXT REFERENCES sales_reps(rep_id),
    confidence        NUMERIC(4,3) NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 1),
    qualification     TEXT NOT NULL DEFAULT 'unqualified'
        CHECK (qualification IN ('unqualified','qualifying','qualified','disqualified')),
    score             INT NOT NULL DEFAULT 0 CHECK (score BETWEEN 0 AND 100),
    score_breakdown   JSONB NOT NULL DEFAULT '{}',
    contact           JSONB NOT NULL DEFAULT '{}',   -- name/email/phone/company/consent flags
    description       TEXT,
    sqft              INT,
    budget_min        INT,
    budget_max        INT,
    timeline          TEXT NOT NULL DEFAULT 'flexible',
    referred_by       TEXT,
    deal_amount       INT,
    duplicate_of      TEXT,
    lost_reason       TEXT,
    tags              TEXT[] NOT NULL DEFAULT '{}',
    stage_entered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sales_opp_rep_stage ON sales_opportunities(assigned_rep, pipeline_stage);
CREATE INDEX IF NOT EXISTS idx_sales_opp_next_due  ON sales_opportunities(next_action_due)
    WHERE pipeline_stage NOT IN ('closed_won','closed_lost');
CREATE INDEX IF NOT EXISTS idx_sales_opp_contact   ON sales_opportunities(contact_id);
CREATE INDEX IF NOT EXISTS idx_sales_opp_deal      ON sales_opportunities(deal_id);

-- Touch history (inbound / outbound, any channel) -------------------------------
CREATE TABLE IF NOT EXISTS sales_touches (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    opportunity_id    TEXT NOT NULL REFERENCES sales_opportunities(opportunity_id),
    at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    direction         TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
    channel           TEXT NOT NULL,
    by_rep            TEXT,                          -- rep_id or agent name
    summary           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sales_touch_opp ON sales_touches(opportunity_id, at DESC);

-- Human gate: proposed / parked / approved / executed actions -------------------
CREATE TABLE IF NOT EXISTS sales_actions (
    action_id         TEXT PRIMARY KEY,
    action_type       TEXT NOT NULL,
    agent             TEXT NOT NULL,
    opportunity_id    TEXT REFERENCES sales_opportunities(opportunity_id),
    rep_id            TEXT REFERENCES sales_reps(rep_id),
    gate              TEXT NOT NULL CHECK (gate IN ('autonomous','human_gated')),
    approval_level    TEXT NOT NULL CHECK (approval_level IN ('L0','L1','L2','L3','L4')),
    status            TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed','executed','parked','approved','rejected','failed')),
    channel           TEXT,
    rationale         TEXT NOT NULL DEFAULT '',
    payload           JSONB NOT NULL DEFAULT '{}',
    human_signature   TEXT,
    approved_by       TEXT,
    rejected_reason   TEXT,
    truth_label       TEXT NOT NULL DEFAULT 'STAGED',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at       TIMESTAMPTZ,
    -- a human-gated action can only be executed with a signature
    CONSTRAINT ck_gated_needs_signature
        CHECK (gate = 'autonomous' OR status <> 'executed' OR human_signature IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_sales_actions_pending ON sales_actions(rep_id, created_at)
    WHERE status = 'parked';

-- Rep learning loop (closed won/lost feeds back per rep) ---------------------------
CREATE TABLE IF NOT EXISTS sales_rep_learning (
    rep_id              TEXT PRIMARY KEY REFERENCES sales_reps(rep_id),
    leads_assigned      INT NOT NULL DEFAULT 0,
    responded           INT NOT NULL DEFAULT 0,
    appointments        INT NOT NULL DEFAULT 0,
    estimates           INT NOT NULL DEFAULT 0,
    won                 INT NOT NULL DEFAULT 0,
    lost                INT NOT NULL DEFAULT 0,
    response_minutes    NUMERIC[] NOT NULL DEFAULT '{}',
    won_by_source       JSONB NOT NULL DEFAULT '{}',
    lost_by_source      JSONB NOT NULL DEFAULT '{}',
    won_by_project_type JSONB NOT NULL DEFAULT '{}',
    lost_reasons        JSONB NOT NULL DEFAULT '{}',
    activity            JSONB NOT NULL DEFAULT '{}',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Sales sub-agent registry (mirrors core/sales/agents.py, seeded on startup) ------
CREATE TABLE IF NOT EXISTS sales_agent_registry (
    agent_name        TEXT PRIMARY KEY,
    responsibility    TEXT NOT NULL,
    stages            TEXT[] NOT NULL DEFAULT '{}',
    needs_input       TEXT,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO sales_agent_registry (agent_name, responsibility) VALUES
    ('lead_router',           'Capture, dedupe, qualify, score, assign and stage inbound leads'),
    ('followup_agent',        'Track callbacks, stale leads and follow-up cadence'),
    ('outreach_agent',        'Draft SMS, email and social outreach'),
    ('meeting_prep',          'Build pre-call/site-visit briefs from customer + project history'),
    ('account_research',      'Research homeowner/company/property/account context'),
    ('pipeline_analyst',      'Flag stalled deals, missing next actions and pipeline risk'),
    ('estimate_agent',        'Prepare scope, estimate/proposal inputs, allowances and exclusions'),
    ('call_recap_agent',      'Convert calls/meetings into notes, tasks and next actions'),
    ('sales_coach',           'Track rep activity, conversion, response speed and coaching opportunities'),
    ('compliance_data_agent', 'Dedupe, required-field checks, consent/data quality and sensitive-data controls')
ON CONFLICT (agent_name) DO NOTHING;
