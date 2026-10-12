-- Every accepted event, as received: the source of truth. Typed tables below are projections of it
-- (rebuildable from here), filled by the handlers in store.py.
CREATE TABLE events (
    event_id       uuid PRIMARY KEY,                    -- client-generated idempotency key
    type           text        NOT NULL,
    occurred_at    timestamptz NOT NULL,                -- client clock
    received_at    timestamptz NOT NULL DEFAULT now(),  -- server clock
    username       text        NOT NULL,
    source_name    text        NOT NULL,
    source_version text        NOT NULL,
    data           jsonb       NOT NULL
);
CREATE INDEX events_type_occurred_at ON events (type, occurred_at);
CREATE INDEX events_username ON events (username);

-- One row per cluster: the facts from cluster.created, plus lifecycle times. Events can arrive in
-- any order (spooled clients), so any event for a cluster creates its row; `created_at IS NULL` =
-- its created event is missing. Other cluster tables refer here by cluster_id (normalized).
CREATE TABLE clusters (
    cluster_id         uuid PRIMARY KEY,
    cluster_type       text,                 -- localdev | ephemeral | durable
    cluster_role       text,                 -- dev | test | stage | prod | perftest
    username           text NOT NULL,
    created_at         timestamptz,
    destroyed_at       timestamptz,
    last_seen_at       timestamptz,          -- latest occurred_at of any event for the cluster
    platform           text,
    platform_version   text,
    kubernetes_version text,
    arch               text,
    profile            text,                 -- localdev only (as created)
    components         text[],               -- localdev only (as created)
    os                 text,                 -- localdev only: the developer's OS
    os_variant         text,
    os_version         text,
    created_event_id   uuid REFERENCES events,
    destroyed_event_id uuid REFERENCES events
);
CREATE INDEX clusters_type_role ON clusters (cluster_type, cluster_role);
CREATE INDEX clusters_created_at ON clusters (created_at);
CREATE INDEX clusters_last_seen_at ON clusters (last_seen_at);
CREATE INDEX clusters_username ON clusters (username);

-- Ad-hoc events attached to a cluster (app.deployed, tests.run, profile.changed, ...).
CREATE TABLE cluster_events (
    event_id    uuid PRIMARY KEY REFERENCES events,
    cluster_id  uuid        NOT NULL REFERENCES clusters,
    occurred_at timestamptz NOT NULL,
    username    text        NOT NULL,
    name        text        NOT NULL,
    outcome     text,
    duration_ms bigint,
    attributes  jsonb       NOT NULL DEFAULT '{}'
);
CREATE INDEX cluster_events_cluster ON cluster_events (cluster_id, occurred_at);
CREATE INDEX cluster_events_name ON cluster_events (name, occurred_at);

-- Periodic cluster size samples (cluster.heartbeat).
CREATE TABLE cluster_heartbeats (
    event_id     uuid PRIMARY KEY REFERENCES events,
    cluster_id   uuid        NOT NULL REFERENCES clusters,
    occurred_at  timestamptz NOT NULL,
    nodes        integer     NOT NULL,
    cores        numeric     NOT NULL,
    memory_bytes bigint      NOT NULL,
    pods         integer     NOT NULL,
    namespaces   integer     NOT NULL
);
CREATE INDEX cluster_heartbeats_cluster ON cluster_heartbeats (cluster_id, occurred_at);
