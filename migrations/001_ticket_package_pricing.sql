-- TIKETI360: configurable package pricing
-- No prices or discounts are inserted by this migration.

CREATE TABLE IF NOT EXISTS ticket_package_pricing (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL,
    category_code VARCHAR(30) NOT NULL,
    wave VARCHAR(20) NOT NULL
        CHECK (wave IN ('LEGACY', 'WAVE1', 'WAVE2')),
    package_code VARCHAR(20) NOT NULL
        CHECK (package_code IN (
            'SOLO', 'COUPLE', 'FAMILY3',
            'FAMILY4', 'GROUP5', 'GROUP10'
        )),
    package_price BIGINT
        CHECK (package_price IS NULL OR package_price >= 0),
    discount_amount BIGINT
        CHECK (discount_amount IS NULL OR discount_amount >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    -- Use either a final package price or a discount, not both.
    CHECK (
        package_price IS NULL
        OR discount_amount IS NULL
    ),

    UNIQUE (event_id, category_code, wave, package_code)
);

ALTER TABLE bookings
ADD COLUMN IF NOT EXISTS package_code VARCHAR(20);
