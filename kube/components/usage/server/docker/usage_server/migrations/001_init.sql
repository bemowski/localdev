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

-- One row per localdev cluster. Created/destroyed/ad-hoc events can arrive in any order (spooled
-- clients), so any of them creates the row; `created_at IS NULL` = its created event is missing.
CREATE TABLE localdev_clusters (
    cluster_id         uuid PRIMARY KEY,
    username           text NOT NULL,
    created_at         timestamptz,
    destroyed_at       timestamptz,
    profile            text,
    components         text[],
    platform           text,
    platform_version   text,
    kubernetes_version text,
    os                 text,
    os_variant         text,
    os_version         text,
    arch               text,
    created_event_id   uuid REFERENCES events,
    destroyed_event_id uuid REFERENCES events
);
CREATE INDEX localdev_clusters_created_at ON localdev_clusters (created_at);
CREATE INDEX localdev_clusters_username ON localdev_clusters (username);

-- Ad-hoc events attached to a cluster (app.deployed, tests.run, ...).
CREATE TABLE localdev_cluster_events (
    event_id    uuid PRIMARY KEY REFERENCES events,
    cluster_id  uuid        NOT NULL REFERENCES localdev_clusters,
    occurred_at timestamptz NOT NULL,
    username    text        NOT NULL,
    name        text        NOT NULL,
    outcome     text,
    duration_ms bigint,
    attributes  jsonb       NOT NULL DEFAULT '{}'
);
CREATE INDEX localdev_cluster_events_cluster ON localdev_cluster_events (cluster_id, occurred_at);
CREATE INDEX localdev_cluster_events_name ON localdev_cluster_events (name, occurred_at);
