-- StyleNow · Supabase (PostgreSQL) Schema
-- Run via: psql $DATABASE_URL -f schema.sql
-- Or paste into Supabase SQL Editor.
-- All statements are idempotent (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).

-- ─────────────────────────────────────────
-- Extensions
-- ─────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trigram similarity for fuzzy search

-- ─────────────────────────────────────────
-- Auth / Users
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    phone           TEXT UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    email           TEXT NOT NULL DEFAULT '',
    picture         TEXT NOT NULL DEFAULT '',
    auth_provider   TEXT NOT NULL DEFAULT 'phone',
    theme_preference TEXT NOT NULL DEFAULT 'system',
    disabled        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS users_email_idx ON users(email);
CREATE INDEX IF NOT EXISTS users_created_at_idx ON users(created_at);

CREATE TABLE IF NOT EXISTS admin_users (
    id              TEXT PRIMARY KEY,
    email           TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    password_hash   TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'super_admin',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    theme_preference TEXT NOT NULL DEFAULT 'system',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS otp_requests (
    id          TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,
    otp         TEXT NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    consumed    BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS otp_phone_idx ON otp_requests(phone, consumed);

CREATE TABLE IF NOT EXISTS login_attempts (
    id          TEXT PRIMARY KEY,   -- "{ip}:{email}"
    count       INT NOT NULL DEFAULT 0,
    locked_until TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL DEFAULT 'google',
    session_token   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- ─────────────────────────────────────────
-- Catalog
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS categories (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    slug    TEXT UNIQUE NOT NULL,
    image   TEXT NOT NULL DEFAULT '',
    active  BOOLEAN NOT NULL DEFAULT TRUE,
    sort    INT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS categories_active_sort_idx ON categories(active, sort);

CREATE TABLE IF NOT EXISTS products (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    category_id     TEXT NOT NULL DEFAULT '',
    category_name   TEXT NOT NULL DEFAULT '',
    category_slug   TEXT NOT NULL DEFAULT '',
    subcategory     TEXT NOT NULL DEFAULT '',
    brand           TEXT NOT NULL DEFAULT '',
    gender          TEXT NOT NULL DEFAULT '',
    material        TEXT NOT NULL DEFAULT '',
    fabric          TEXT NOT NULL DEFAULT '',
    tags            TEXT[] NOT NULL DEFAULT '{}',
    images          TEXT[] NOT NULL DEFAULT '{}',
    variants        JSONB NOT NULL DEFAULT '[]',
    seo_title       TEXT NOT NULL DEFAULT '',
    seo_description TEXT NOT NULL DEFAULT '',
    attributes      JSONB NOT NULL DEFAULT '{}',
    featured        BOOLEAN NOT NULL DEFAULT FALSE,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    out_of_stock    BOOLEAN NOT NULL DEFAULT FALSE,
    order_count     INT NOT NULL DEFAULT 0,
    rating_avg      FLOAT NOT NULL DEFAULT 0,
    rating_count    INT NOT NULL DEFAULT 0,
    search_vec      TSVECTOR,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS products_active_idx ON products(active);
CREATE INDEX IF NOT EXISTS products_category_idx ON products(category_id, active);
CREATE INDEX IF NOT EXISTS products_featured_idx ON products(featured, active);
CREATE INDEX IF NOT EXISTS products_created_at_idx ON products(created_at DESC);
CREATE INDEX IF NOT EXISTS products_order_count_idx ON products(order_count DESC);
CREATE INDEX IF NOT EXISTS products_search_vec_idx ON products USING GIN(search_vec);
CREATE INDEX IF NOT EXISTS products_tags_idx ON products USING GIN(tags);
CREATE INDEX IF NOT EXISTS products_name_trgm_idx ON products USING GIN(name gin_trgm_ops);

-- Trigger to keep search_vec updated
CREATE OR REPLACE FUNCTION products_search_vec_update() RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vec := to_tsvector('english',
        coalesce(NEW.name, '') || ' ' ||
        coalesce(NEW.brand, '') || ' ' ||
        coalesce(NEW.description, '') || ' ' ||
        coalesce(array_to_string(NEW.tags, ' '), '')
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS products_search_vec_trigger ON products;
CREATE TRIGGER products_search_vec_trigger
    BEFORE INSERT OR UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION products_search_vec_update();

-- ─────────────────────────────────────────
-- Files & Videos
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS files (
    id                  TEXT PRIMARY KEY,
    storage_path        TEXT NOT NULL,
    original_filename   TEXT NOT NULL DEFAULT '',
    content_type        TEXT NOT NULL DEFAULT '',
    size                INT NOT NULL DEFAULT 0,
    kind                TEXT NOT NULL DEFAULT 'image',
    public              BOOLEAN NOT NULL DEFAULT TRUE,
    is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_storage_path_idx ON files(storage_path, is_deleted);
CREATE INDEX IF NOT EXISTS files_kind_idx ON files(kind, is_deleted);

CREATE TABLE IF NOT EXISTS videos (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL DEFAULT '',
    caption     TEXT NOT NULL DEFAULT '',
    product_id  TEXT NOT NULL DEFAULT '',
    video       TEXT NOT NULL DEFAULT '',
    poster      TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'review',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    sort        INT NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_product_idx ON videos(product_id, active);
CREATE INDEX IF NOT EXISTS videos_active_sort_idx ON videos(active, sort);

-- ─────────────────────────────────────────
-- Shopping: Cart, Wishlist, Addresses
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS carts (
    key         TEXT PRIMARY KEY,   -- "user:{id}" or "guest:{uuid}"
    items       JSONB NOT NULL DEFAULT '[]',
    coupon_code TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wishlists (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    product_ids TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS addresses (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    line1       TEXT NOT NULL DEFAULT '',
    line2       TEXT NOT NULL DEFAULT '',
    landmark    TEXT NOT NULL DEFAULT '',
    city        TEXT NOT NULL DEFAULT 'Bahraich',
    pincode     TEXT NOT NULL DEFAULT '',
    is_default  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS addresses_user_idx ON addresses(user_id);

-- ─────────────────────────────────────────
-- Orders & Returns
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS orders (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    customer                JSONB NOT NULL DEFAULT '{}',
    items                   JSONB NOT NULL DEFAULT '[]',
    subtotal                FLOAT NOT NULL DEFAULT 0,
    discount                FLOAT NOT NULL DEFAULT 0,
    coupon_code             TEXT,
    delivery_fee            FLOAT NOT NULL DEFAULT 0,
    total                   FLOAT NOT NULL DEFAULT 0,
    address                 JSONB NOT NULL DEFAULT '{}',
    note                    TEXT NOT NULL DEFAULT '',
    points_redeemed         INT NOT NULL DEFAULT 0,
    points_discount         FLOAT NOT NULL DEFAULT 0,
    try_at_doorstep         JSONB NOT NULL DEFAULT '{}',
    status                  TEXT NOT NULL DEFAULT 'placed',
    payment_status          TEXT NOT NULL DEFAULT 'pending',
    payment_method          TEXT NOT NULL DEFAULT 'online',
    payment_id              TEXT,
    razorpay_order_id       TEXT,
    rider                   JSONB,
    internal_notes          JSONB NOT NULL DEFAULT '[]',
    reward_points_awarded   INT NOT NULL DEFAULT 0,
    eta                     TEXT NOT NULL DEFAULT '',
    timeline                JSONB NOT NULL DEFAULT '[]',
    refund_method           TEXT,
    refund_details          JSONB,
    refund_status           TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id);
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders(status);
CREATE INDEX IF NOT EXISTS orders_created_at_idx ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS orders_razorpay_idx ON orders(razorpay_order_id) WHERE razorpay_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS returns (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reason          TEXT NOT NULL DEFAULT '',
    items           JSONB NOT NULL DEFAULT '[]',
    amount          FLOAT NOT NULL DEFAULT 0,
    refund_method   TEXT NOT NULL DEFAULT 'cash',
    refund_details  JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'requested',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS returns_user_idx ON returns(user_id);
CREATE INDEX IF NOT EXISTS returns_order_idx ON returns(order_id);
CREATE INDEX IF NOT EXISTS returns_status_idx ON returns(status);

CREATE TABLE IF NOT EXISTS inventory_transactions (
    id          TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    variant_id  TEXT NOT NULL,
    change      INT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    ref         TEXT NOT NULL DEFAULT '',
    by          TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inv_txn_product_idx ON inventory_transactions(product_id);

-- ─────────────────────────────────────────
-- Rewards
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reward_accounts (
    id      TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    balance INT NOT NULL DEFAULT 0,
    earned  INT NOT NULL DEFAULT 0,
    used    INT NOT NULL DEFAULT 0,
    expired INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reward_transactions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    points      INT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    ref         TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reward_txn_user_idx ON reward_transactions(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS spin_rewards (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL DEFAULT 'none',
    value       FLOAT NOT NULL DEFAULT 0,
    probability FLOAT NOT NULL DEFAULT 1,
    expiry_days INT NOT NULL DEFAULT 7,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spin_transactions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reward_label    TEXT NOT NULL DEFAULT '',
    reward_type     TEXT NOT NULL DEFAULT 'none',
    reward_value    FLOAT NOT NULL DEFAULT 0,
    coupon_code     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spin_txn_user_idx ON spin_transactions(user_id, created_at DESC);

-- ─────────────────────────────────────────
-- Coupons
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS coupons (
    id              TEXT PRIMARY KEY,
    code            TEXT UNIQUE NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    type            TEXT NOT NULL DEFAULT 'percent',
    value           FLOAT NOT NULL DEFAULT 0,
    min_order       FLOAT NOT NULL DEFAULT 0,
    max_discount    FLOAT,
    usage_limit     INT,
    per_user_limit  INT DEFAULT 1,
    used_count      INT NOT NULL DEFAULT 0,
    expires_at      TEXT NOT NULL DEFAULT '',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    first_order_only BOOLEAN NOT NULL DEFAULT FALSE,
    user_id         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS coupons_code_idx ON coupons(code);
CREATE INDEX IF NOT EXISTS coupons_user_idx ON coupons(user_id) WHERE user_id IS NOT NULL;

-- ─────────────────────────────────────────
-- CMS: Settings, Banners, Homepage, Deals
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS settings (
    id                          TEXT PRIMARY KEY,   -- always "global"
    city                        TEXT NOT NULL DEFAULT 'Bahraich',
    delivery_fee                FLOAT NOT NULL DEFAULT 0,
    delivery_eta_min            INT NOT NULL DEFAULT 30,
    delivery_eta_max            INT NOT NULL DEFAULT 60,
    points_per_spin             INT NOT NULL DEFAULT 50,
    points_per_rupee            FLOAT NOT NULL DEFAULT 0.05,
    points_value_rupee          FLOAT NOT NULL DEFAULT 1,
    low_stock_threshold         INT NOT NULL DEFAULT 5,
    spin_enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    brand_accent                TEXT NOT NULL DEFAULT '#BD8EE4',
    social_links                JSONB NOT NULL DEFAULT '{}',
    contact_phones              JSONB NOT NULL DEFAULT '[]',
    try_at_doorstep_enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    try_at_doorstep_threshold   FLOAT NOT NULL DEFAULT 499,
    try_at_doorstep_fee         FLOAT NOT NULL DEFAULT 50,
    created_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS banners (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL DEFAULT '',
    subtitle TEXT NOT NULL DEFAULT '',
    image    TEXT NOT NULL DEFAULT '',
    link     TEXT NOT NULL DEFAULT '',
    active   BOOLEAN NOT NULL DEFAULT TRUE,
    sort     INT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS banners_active_sort_idx ON banners(active, sort);

CREATE TABLE IF NOT EXISTS homepage_deals (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL DEFAULT '',
    icon        TEXT NOT NULL DEFAULT '',
    link        TEXT NOT NULL DEFAULT '',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    sort        INT NOT NULL DEFAULT 0,
    start_at    TEXT NOT NULL DEFAULT '',
    end_at      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS homepage_deals_sort_idx ON homepage_deals(sort);

CREATE TABLE IF NOT EXISTS homepage (
    id          TEXT PRIMARY KEY,   -- always "homepage"
    sections    JSONB NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS deals (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    discount_pct    FLOAT NOT NULL DEFAULT 0,
    product_ids     TEXT[] NOT NULL DEFAULT '{}',
    category_id     TEXT NOT NULL DEFAULT '',
    start_at        TEXT NOT NULL DEFAULT '',
    end_at          TEXT NOT NULL DEFAULT '',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deals_active_idx ON deals(active);

CREATE TABLE IF NOT EXISTS stores (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    city        TEXT NOT NULL DEFAULT '',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    radius_km   FLOAT NOT NULL DEFAULT 10,
    eta         TEXT NOT NULL DEFAULT '30-60 min',
    created_at  TEXT NOT NULL
);

-- ─────────────────────────────────────────
-- Analytics & Audit
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS search_logs (
    id              TEXT PRIMARY KEY,
    query           TEXT NOT NULL,
    results         INT NOT NULL DEFAULT 0,
    user_id         TEXT,
    clicked_product TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS search_logs_query_idx ON search_logs(query);
CREATE INDEX IF NOT EXISTS search_logs_created_at_idx ON search_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS search_synonyms (
    id          TEXT PRIMARY KEY,
    keyword     TEXT UNIQUE NOT NULL,
    synonyms    TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    data        JSONB NOT NULL DEFAULT '{}',
    read        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_user_idx ON notifications(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS recently_viewed (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id  TEXT NOT NULL,
    viewed_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, product_id)
);
CREATE INDEX IF NOT EXISTS recently_viewed_user_idx ON recently_viewed(user_id, viewed_at DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          TEXT PRIMARY KEY,
    admin_id    TEXT NOT NULL,
    admin_email TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    previous    JSONB,
    new         JSONB,
    ip          TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_logs_created_at_idx ON audit_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS delivery_partners (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    zone        TEXT NOT NULL DEFAULT '',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id          TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_name   TEXT NOT NULL DEFAULT '',
    rating      INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment     TEXT NOT NULL DEFAULT '',
    images      TEXT[] NOT NULL DEFAULT '{}',
    approved    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TEXT NOT NULL,
    UNIQUE (product_id, user_id)
);
CREATE INDEX IF NOT EXISTS reviews_product_idx ON reviews(product_id, approved);

-- ─────────────────────────────────────────
-- Atomic SQL Functions
-- ─────────────────────────────────────────

-- Atomically decrement variant stock; returns updated variant JSONB or NULL if insufficient stock
CREATE OR REPLACE FUNCTION decrement_variant_stock(
    p_product_id TEXT,
    p_variant_id TEXT,
    p_qty        INT
) RETURNS JSONB AS $$
DECLARE
    v_variants  JSONB;
    v_variant   JSONB;
    v_new_var   JSONB;
    v_new_vars  JSONB;
    i           INT;
    v_idx       INT := -1;
BEGIN
    SELECT variants INTO v_variants
    FROM products WHERE id = p_product_id FOR UPDATE;

    IF v_variants IS NULL THEN
        RETURN NULL;
    END IF;

    -- Find variant index
    FOR i IN 0 .. jsonb_array_length(v_variants) - 1 LOOP
        IF v_variants -> i ->> 'id' = p_variant_id THEN
            v_idx := i;
            EXIT;
        END IF;
    END LOOP;

    IF v_idx < 0 THEN RETURN NULL; END IF;

    v_variant := v_variants -> v_idx;

    IF (v_variant ->> 'stock')::INT < p_qty THEN
        RETURN NULL;
    END IF;

    v_new_var := jsonb_set(v_variant, '{stock}',
        to_jsonb((v_variant ->> 'stock')::INT - p_qty));

    -- Rebuild variants array
    v_new_vars := '[]'::JSONB;
    FOR i IN 0 .. jsonb_array_length(v_variants) - 1 LOOP
        IF i = v_idx THEN
            v_new_vars := v_new_vars || jsonb_build_array(v_new_var);
        ELSE
            v_new_vars := v_new_vars || jsonb_build_array(v_variants -> i);
        END IF;
    END LOOP;

    UPDATE products SET variants = v_new_vars WHERE id = p_product_id;
    RETURN v_new_var;
END;
$$ LANGUAGE plpgsql;


-- Atomically increment variant stock (used for cancellations / rollbacks)
CREATE OR REPLACE FUNCTION increment_variant_stock(
    p_product_id TEXT,
    p_variant_id TEXT,
    p_qty        INT
) RETURNS VOID AS $$
DECLARE
    v_variants  JSONB;
    v_variant   JSONB;
    v_new_var   JSONB;
    v_new_vars  JSONB;
    i           INT;
    v_idx       INT := -1;
BEGIN
    SELECT variants INTO v_variants
    FROM products WHERE id = p_product_id FOR UPDATE;

    IF v_variants IS NULL THEN RETURN; END IF;

    FOR i IN 0 .. jsonb_array_length(v_variants) - 1 LOOP
        IF v_variants -> i ->> 'id' = p_variant_id THEN
            v_idx := i;
            EXIT;
        END IF;
    END LOOP;

    IF v_idx < 0 THEN RETURN; END IF;

    v_variant := v_variants -> v_idx;
    v_new_var := jsonb_set(v_variant, '{stock}',
        to_jsonb((v_variant ->> 'stock')::INT + p_qty));

    v_new_vars := '[]'::JSONB;
    FOR i IN 0 .. jsonb_array_length(v_variants) - 1 LOOP
        IF i = v_idx THEN
            v_new_vars := v_new_vars || jsonb_build_array(v_new_var);
        ELSE
            v_new_vars := v_new_vars || jsonb_build_array(v_variants -> i);
        END IF;
    END LOOP;

    UPDATE products SET variants = v_new_vars WHERE id = p_product_id;
END;
$$ LANGUAGE plpgsql;
