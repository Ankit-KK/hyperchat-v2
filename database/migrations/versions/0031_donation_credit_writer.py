"""PAY-15 (D17) stage 2: per-donation credit columns, the capped backfill and the one donor-total writer.

Revision ID: 0031
Revises: 0030

Until now three code paths (payment completion, dashboard moderation, bot moderation) changed
streamer_donator_totals themselves, debits floored at 0. A floored debit loses the exact amount, and a later credit
then adds back more than was removed (PAY-15 plan section 14, design model). From this revision:

* donations.creditable_inr  the most a donation may ever contribute to its donor total. NULL means amount_inr (every
                            new donation). Set once, here, for existing paid and approved rows.
* donations.credited_inr    what it contributes now: the sum of every change the writer applied for it.
* donations_credit_writer   ONE BEFORE INSERT OR UPDATE trigger on donations, for every role and every path:
                            target = (paid AND approved) ? COALESCE(creditable_inr, amount_inr) : 0;
                            delta = target - credited_inr is applied to the donor total in the same transaction,
                            under the donation's row lock and the total's row lock. A debit larger than the total
                            FAILS CLOSED (SQLSTATE HC002, nothing written) instead of flooring. Runtime roles can
                            never set either credit column, nor can the owner: only the backfill (below) does, by
                            disabling this trigger for its own UPDATE under the table lock. For runtime roles the
                            financial fields are fixed: amount, amount_inr, currency, streamer_id, donor_name, a set
                            provider_payment_id, and a paid status (pending / failed -> success stays allowed).
                            donation_count keeps its existing meaning (+1 / -1 when a donation starts / stops
                            contributing, floored at 0): it is not shown anywhere and not money.
                            Only public.donations_credit_backfill() (owner, one transaction) may set the credit
                            columns directly; there is no other manual path yet (an audited correction CLI is a
                            later stage).
* donation_credit_events    append-only audit (every role, the owner included; TRUNCATE only while empty): one row per
                            backfilled credit (cause 'backfill', the total was not changed) and per writer change
                            (cause 'writer', with the total afterwards). Per donation, credited_inr = the sum of its
                            rows' delta_inr. No foreign key: the audit outlives a deleted donation.
* donations_credit_delete_guard
                            a donation with credited_inr <> 0 cannot be deleted or truncated by any role (reject it
                            first: the writer debits it). Cleanup of whole synthetic creators and scratch databases
                            disables the trigger explicitly as the owner, in one transaction.
* streamer_donator_totals   runtime roles keep SELECT only: the writer (SECURITY DEFINER) is the only path that
                            changes a total.

Backfill (public.donations_credit_backfill(); also run by the cutover ETL after its load, refused once any audit
row exists): per (creator, lower(donor name)) bucket, paid and approved donations are credited V2 rows first, then
imported rows, oldest first, each min(amount_inr, what is left of the bucket); creditable_inr = that credit. A
bucket that holds less than its donations ("short") leaves the newest rows partly or not credited, so a total can
never be debited below what it really holds. Existing donor totals are NOT changed. Nothing here applies a refund
or dispute: evidence-based reversals are a later stage (the full reversal ledger, behind its switch).

Downgrade refuses while any audit row exists; otherwise it removes the writer, the delete guard, the audit table,
the backfill function and both columns, and restores the 0007 grants on streamer_donator_totals.
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

RUNTIME_ROLES = ("('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup', "
                 "'hyperchat_readonly')")
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")
WRITERS = "hyperchat_app, hyperchat_worker"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.donations
            ADD COLUMN creditable_inr numeric(12,2),
            ADD COLUMN credited_inr numeric(12,2) NOT NULL DEFAULT 0,
            ADD CONSTRAINT donations_creditable_bounds
                CHECK (creditable_inr IS NULL OR (creditable_inr >= 0 AND creditable_inr <= amount_inr)),
            ADD CONSTRAINT donations_credited_bounds
                CHECK (credited_inr >= 0 AND credited_inr <= COALESCE(creditable_inr, amount_inr))
    """)
    op.execute("""
        CREATE TABLE public.donation_credit_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            donation_id uuid NOT NULL,
            streamer_id uuid NOT NULL,
            donator_name_lower text NOT NULL,
            cause text NOT NULL CHECK (cause IN ('backfill', 'writer')),
            payment_status text NOT NULL,
            moderation_status text NOT NULL,
            credited_before numeric(12,2) NOT NULL,
            credited_after numeric(12,2) NOT NULL,
            delta_inr numeric(12,2) NOT NULL,
            delta_count smallint NOT NULL CHECK (delta_count IN (-1, 0, 1)),
            total_after numeric(12,2),
            actor text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT donation_credit_events_delta CHECK (delta_inr = credited_after - credited_before),
            CONSTRAINT donation_credit_events_moves CHECK (delta_inr <> 0 OR delta_count <> 0),
            CONSTRAINT donation_credit_events_total CHECK ((cause = 'writer') = (total_after IS NOT NULL))
        )
    """)
    op.execute("CREATE INDEX donation_credit_events_donation_idx ON public.donation_credit_events (donation_id, id)")
    op.execute("""
        CREATE FUNCTION public.donation_credit_events_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        DECLARE
            v_has_rows boolean;
        BEGIN
            IF TG_OP = 'TRUNCATE' THEN
                SELECT EXISTS (SELECT 1 FROM public.donation_credit_events) INTO v_has_rows;
                IF NOT v_has_rows THEN
                    RETURN NULL;
                END IF;
            END IF;
            RAISE EXCEPTION 'donation credit audit is append-only (% refused)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END $$
    """)
    op.execute("CREATE TRIGGER donation_credit_events_guard BEFORE UPDATE OR DELETE ON public.donation_credit_events "
               "FOR EACH ROW EXECUTE FUNCTION public.donation_credit_events_guard()")
    op.execute("CREATE TRIGGER donation_credit_events_guard_truncate BEFORE TRUNCATE ON public.donation_credit_events "
               "FOR EACH STATEMENT EXECUTE FUNCTION public.donation_credit_events_guard()")

    # The backfill: before the writer exists, so it can never move a total.
    op.execute("""
        CREATE FUNCTION public.donations_credit_backfill(OUT credited bigint, OUT short_v2 bigint,
                                                         OUT short_imported bigint, OUT short_buckets bigint)
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.donation_credit_events) THEN
                RAISE EXCEPTION 'credit backfill refused: the credit audit already has rows (it runs once, on a '
                                'database the writer has not touched)' USING ERRCODE = 'object_not_in_prerequisite_state';
            END IF;
            -- the writer refuses every direct credit-column change; only this function lifts it, for its own
            -- UPDATE, holding the table's ACCESS EXCLUSIVE lock until commit (no other write can slip in)
            ALTER TABLE public.donations DISABLE TRIGGER donations_set_updated_at,  -- no row looks modified
                                         DISABLE TRIGGER donations_credit_writer;
            CREATE TEMP TABLE pg_temp.credit_alloc ON COMMIT DROP AS
                SELECT r.id, r.streamer_id, r.name, r.imported, r.amount_inr, r.payment_status, r.moderation_status,
                       GREATEST(0, LEAST(r.amount_inr, r.bucket - (r.running - r.amount_inr)))::numeric(12,2) AS credit
                FROM (SELECT d.id, d.streamer_id, lower(d.donor_name) AS name, d.amount_inr, d.payment_status,
                             d.moderation_status, d.legacy_source_table IS NOT NULL AS imported,
                             COALESCE(t.total_amount, 0) AS bucket,
                             sum(d.amount_inr) OVER (PARTITION BY d.streamer_id, lower(d.donor_name)
                                                     ORDER BY d.legacy_source_table IS NOT NULL, d.created_at, d.id
                                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running
                      FROM public.donations d
                      LEFT JOIN public.streamer_donator_totals t
                             ON t.streamer_id = d.streamer_id AND t.donator_name_lower = lower(d.donor_name)
                      WHERE d.payment_status = 'success' AND d.moderation_status IN ('approved', 'auto_approved')) r;
            UPDATE public.donations d SET creditable_inr = a.credit, credited_inr = a.credit
                FROM pg_temp.credit_alloc a WHERE d.id = a.id;
            INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause,
                    payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count,
                    total_after, actor)
                SELECT id, streamer_id, name, 'backfill', payment_status, moderation_status, 0, credit, credit, 0, NULL,
                       session_user
                FROM pg_temp.credit_alloc WHERE credit > 0 ORDER BY streamer_id, name, imported, id;
            ALTER TABLE public.donations ENABLE TRIGGER donations_set_updated_at,
                                         ENABLE TRIGGER donations_credit_writer;
            SELECT count(*) FILTER (WHERE credit > 0),
                   count(*) FILTER (WHERE credit < amount_inr AND NOT imported),
                   count(*) FILTER (WHERE credit < amount_inr AND imported),
                   count(DISTINCT streamer_id::text || ':' || name) FILTER (WHERE credit < amount_inr)
                INTO credited, short_v2, short_imported, short_buckets FROM pg_temp.credit_alloc;
        END $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION public.donations_credit_backfill() FROM {EVERYONE}")

    op.execute(f"""
        CREATE FUNCTION public.donations_credit_writer() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            v_runtime boolean := session_user IN {RUNTIME_ROLES};
            v_before numeric(12,2) := CASE WHEN TG_OP = 'INSERT' THEN 0 ELSE OLD.credited_inr END;
            v_target numeric(12,2);
            v_delta numeric(12,2);
            v_count smallint;
            v_total numeric(12,2);
            v_name text := lower(NEW.donor_name);
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.credited_inr <> 0 OR NEW.creditable_inr IS NOT NULL THEN
                    RAISE EXCEPTION 'donations.credited_inr / creditable_inr are set by the credit writer only'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
            ELSIF NEW.credited_inr IS DISTINCT FROM OLD.credited_inr
                  OR NEW.creditable_inr IS DISTINCT FROM OLD.creditable_inr THEN
                -- every role, the owner included: only public.donations_credit_backfill() (which disables this
                -- trigger for its own UPDATE) sets them
                RAISE EXCEPTION 'donations.credited_inr / creditable_inr are set by the credit writer only (and once '
                                'by public.donations_credit_backfill())' USING ERRCODE = 'insufficient_privilege';
            ELSIF v_runtime AND (
                    (NEW.amount, NEW.amount_inr, NEW.currency, NEW.streamer_id, NEW.donor_name)
                        IS DISTINCT FROM (OLD.amount, OLD.amount_inr, OLD.currency, OLD.streamer_id, OLD.donor_name)
                    OR (OLD.provider_payment_id IS NOT NULL
                        AND NEW.provider_payment_id IS DISTINCT FROM OLD.provider_payment_id)
                    OR (OLD.payment_status = 'success' AND NEW.payment_status <> 'success')) THEN
                -- financial attribution is fixed at checkout; the payment id is set once (pending / failed ->
                -- success) and a paid donation stays paid. No runtime code path changes any of these.
                RAISE EXCEPTION 'donation amount, currency, creator, donor, payment id and paid status are fixed '
                                'for runtime roles' USING ERRCODE = 'insufficient_privilege';
            ELSIF OLD.credited_inr > 0 AND (NEW.streamer_id IS DISTINCT FROM OLD.streamer_id
                                            OR lower(NEW.donor_name) IS DISTINCT FROM lower(OLD.donor_name)) THEN
                RAISE EXCEPTION 'a credited donation cannot move to another donor total'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;

            v_target := CASE WHEN NEW.payment_status = 'success' AND NEW.moderation_status IN ('approved', 'auto_approved')
                             THEN COALESCE(NEW.creditable_inr, NEW.amount_inr) ELSE 0 END;
            v_delta := v_target - v_before;
            NEW.credited_inr := v_before;
            IF v_delta = 0 THEN
                RETURN NEW;
            END IF;
            v_count := CASE WHEN v_before = 0 THEN 1 WHEN v_target = 0 THEN -1 ELSE 0 END;
            IF v_delta > 0 THEN
                INSERT INTO public.streamer_donator_totals AS t
                        (streamer_id, donator_name, donator_name_lower, total_amount, donation_count)
                    VALUES (NEW.streamer_id, NEW.donor_name, v_name, v_delta, GREATEST(v_count, 0))
                    ON CONFLICT (streamer_id, donator_name_lower) DO UPDATE SET
                        total_amount = t.total_amount + EXCLUDED.total_amount,
                        donation_count = GREATEST(0, t.donation_count + v_count),
                        donator_name = EXCLUDED.donator_name, updated_at = now()
                    RETURNING t.total_amount INTO v_total;
            ELSE
                UPDATE public.streamer_donator_totals t
                    SET total_amount = t.total_amount + v_delta,
                        donation_count = GREATEST(0, t.donation_count + v_count), updated_at = now()
                    WHERE t.streamer_id = NEW.streamer_id AND t.donator_name_lower = v_name
                      AND t.total_amount >= -v_delta
                    RETURNING t.total_amount INTO v_total;
                IF NOT FOUND THEN  -- fail closed: never floor, never lose the exact amount
                    RAISE EXCEPTION 'donor total would go below zero (debit % of donation %)', -v_delta, NEW.id
                        USING ERRCODE = 'HC002';
                END IF;
            END IF;
            INSERT INTO public.donation_credit_events (donation_id, streamer_id, donator_name_lower, cause,
                    payment_status, moderation_status, credited_before, credited_after, delta_inr, delta_count,
                    total_after, actor)
                VALUES (NEW.id, NEW.streamer_id, v_name, 'writer', NEW.payment_status, NEW.moderation_status,
                        v_before, v_target, v_delta, v_count, v_total, session_user);
            NEW.credited_inr := v_target;
            RETURN NEW;
        END $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION public.donations_credit_writer() FROM {EVERYONE}")
    op.execute(f"REVOKE ALL ON FUNCTION public.donation_credit_events_guard() FROM {EVERYONE}")
    op.execute("CREATE TRIGGER donations_credit_writer BEFORE INSERT OR UPDATE ON public.donations "
               "FOR EACH ROW EXECUTE FUNCTION public.donations_credit_writer()")

    # A credited donation is never deleted (or truncated) while it still counts in a donor total: every role, the
    # owner included. Cleanup that removes a whole synthetic creator (whose totals go with it) or a scratch database
    # disables this trigger explicitly as the owner, in its own transaction (runtime roles cannot).
    op.execute("""
        CREATE FUNCTION public.donations_credit_delete_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'TRUNCATE' THEN
                IF EXISTS (SELECT 1 FROM public.donations WHERE credited_inr <> 0) THEN
                    RAISE EXCEPTION 'donations holds credited rows (TRUNCATE refused)'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                RETURN NULL;
            END IF;
            IF OLD.credited_inr <> 0 THEN
                RAISE EXCEPTION 'donation % still counts % in its donor total (DELETE refused; reject it first)',
                    OLD.id, OLD.credited_inr USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN OLD;
        END $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION public.donations_credit_delete_guard() FROM {EVERYONE}")
    op.execute("CREATE TRIGGER donations_credit_delete_guard BEFORE DELETE ON public.donations "
               "FOR EACH ROW EXECUTE FUNCTION public.donations_credit_delete_guard()")
    op.execute("CREATE TRIGGER donations_credit_delete_guard_truncate BEFORE TRUNCATE ON public.donations "
               "FOR EACH STATEMENT EXECUTE FUNCTION public.donations_credit_delete_guard()")

    op.execute("""
        DO $$
        DECLARE r record;
        BEGIN
            SELECT * INTO r FROM public.donations_credit_backfill();
            RAISE NOTICE 'credit backfill: % donations credited; short rows: % V2, % imported; short buckets: %',
                r.credited, r.short_v2, r.short_imported, r.short_buckets;
        END $$
    """)

    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.streamer_donator_totals FROM {WRITERS}")
    op.execute(f"REVOKE ALL ON public.donation_credit_events FROM {WRITERS}")
    op.execute(f"GRANT SELECT ON public.donation_credit_events TO {WRITERS}")


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.donation_credit_events) THEN
                RAISE EXCEPTION 'downgrade of 0031 stopped: the donation credit audit has % row(s) (nothing was '
                                'changed)', (SELECT count(*) FROM public.donation_credit_events);
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER donations_credit_delete_guard_truncate ON public.donations")
    op.execute("DROP TRIGGER donations_credit_delete_guard ON public.donations")
    op.execute("DROP FUNCTION public.donations_credit_delete_guard()")
    op.execute("DROP TRIGGER donations_credit_writer ON public.donations")
    op.execute("DROP FUNCTION public.donations_credit_writer()")
    op.execute("DROP FUNCTION public.donations_credit_backfill()")
    op.execute("DROP TABLE public.donation_credit_events")
    op.execute("DROP FUNCTION public.donation_credit_events_guard()")
    op.execute("ALTER TABLE public.donations DROP CONSTRAINT donations_credited_bounds, "
               "DROP CONSTRAINT donations_creditable_bounds, DROP COLUMN credited_inr, DROP COLUMN creditable_inr")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.streamer_donator_totals TO {WRITERS}")
