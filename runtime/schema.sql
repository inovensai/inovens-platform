CREATE TABLE IF NOT EXISTS platform_users (
 id bigint PRIMARY KEY, email text NOT NULL UNIQUE, name text NOT NULL,
 role text NOT NULL DEFAULT 'member' CHECK(role IN ('owner','president','vice_president','board','member')),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','revoked')),
 telegram_id bigint UNIQUE, daily_limit bigint CHECK(daily_limit>=0),
 consent_version text, consent_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value jsonb NOT NULL);
INSERT INTO settings VALUES ('limits','{"daily_try":30,"monthly_try":1000,"usd_try":50,"pilot_size":10,"enabled":false}') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS conversations (
 id uuid PRIMARY KEY,user_id bigint NOT NULL REFERENCES platform_users(id),
 title text NOT NULL, scope text NOT NULL DEFAULT 'personal' CHECK(scope IN ('personal','board','google')),
 google_bound boolean NOT NULL DEFAULT false, channel text NOT NULL DEFAULT 'web',
 model text NOT NULL DEFAULT 'inovens-combo-fast', created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
 id bigserial PRIMARY KEY,conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 role text NOT NULL CHECK(role IN ('user','assistant')), body text NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jobs (
 id uuid PRIMARY KEY,user_id bigint NOT NULL REFERENCES platform_users(id),conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
 channel text NOT NULL,status text NOT NULL DEFAULT 'queued',model text NOT NULL,prompt text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),started_at timestamptz,finished_at timestamptz,heartbeat_at timestamptz,
 error_code text,call_count int NOT NULL DEFAULT 0,cancel_requested boolean NOT NULL DEFAULT false,
 request_key text NOT NULL UNIQUE, research boolean NOT NULL DEFAULT false
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS display_title text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hidden_at timestamptz;
CREATE INDEX IF NOT EXISTS jobs_user_status ON jobs(user_id,status);
CREATE TABLE IF NOT EXISTS quota_grants (
 id uuid PRIMARY KEY,user_id bigint NOT NULL REFERENCES platform_users(id),job_id uuid REFERENCES jobs(id) ON DELETE CASCADE,
 amount bigint NOT NULL CHECK(amount>0),used bigint NOT NULL DEFAULT 0,reserved bigint NOT NULL DEFAULT 0,
 expires_at timestamptz NOT NULL,created_by bigint NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS daily_usage (
 user_id bigint NOT NULL REFERENCES platform_users(id),day date NOT NULL,
 tokens bigint NOT NULL DEFAULT 0,reserved_tokens bigint NOT NULL DEFAULT 0,
 cost_try numeric(18,8) NOT NULL DEFAULT 0,reserved_try numeric(18,8) NOT NULL DEFAULT 0,
 PRIMARY KEY(user_id,day)
);
CREATE TABLE IF NOT EXISTS model_calls (
 id uuid PRIMARY KEY,job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,user_id bigint NOT NULL,
 day date NOT NULL,month date NOT NULL,model text NOT NULL,provider text NOT NULL,
 status text NOT NULL DEFAULT 'reserved',input_tokens bigint NOT NULL DEFAULT 0,output_tokens bigint NOT NULL DEFAULT 0,
 cached_tokens bigint NOT NULL DEFAULT 0,reasoning_tokens bigint NOT NULL DEFAULT 0,
 reserved_tokens bigint NOT NULL,reserved_try numeric(18,8) NOT NULL,cost_try numeric(18,8),
 quota_reserved bigint NOT NULL,grant_id uuid,grant_reserved bigint NOT NULL DEFAULT 0,
 price jsonb NOT NULL,fx numeric(16,6) NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz,error_code text
);
CREATE INDEX IF NOT EXISTS calls_day ON model_calls(day);
CREATE INDEX IF NOT EXISTS calls_month ON model_calls(month);
CREATE TABLE IF NOT EXISTS documents (
 id uuid PRIMARY KEY,owner_id bigint NOT NULL REFERENCES platform_users(id),name text NOT NULL,
 scope text NOT NULL DEFAULT 'personal' CHECK(scope IN ('personal','community','board','owner')),
 google_source boolean NOT NULL DEFAULT false,body text NOT NULL,content_hash text NOT NULL,
 filename text,mime text NOT NULL DEFAULT 'text/plain',saved boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS document_acl (document_id uuid REFERENCES documents(id) ON DELETE CASCADE,user_id bigint REFERENCES platform_users(id),PRIMARY KEY(document_id,user_id));
CREATE TABLE IF NOT EXISTS document_chunks (
 id bigserial PRIMARY KEY,document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 body text NOT NULL,search_index tsvector NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_search ON document_chunks USING gin(search_index);
CREATE TABLE IF NOT EXISTS operations (
 id bigserial PRIMARY KEY,kind text NOT NULL,title text NOT NULL,owner_name text NOT NULL DEFAULT '',
 start_date date,due_date date,status text NOT NULL DEFAULT 'candidate',note text NOT NULL DEFAULT '',source_ref text UNIQUE,
 scope text NOT NULL DEFAULT 'board',created_by bigint,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE operations ADD COLUMN IF NOT EXISTS start_date date;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS assignee_user_id bigint REFERENCES platform_users(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS operations_assignee_due ON operations(assignee_user_id,due_date) WHERE assignee_user_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS operation_reminders (
 operation_id bigint NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
 user_id bigint NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
 due_date date NOT NULL,kind text NOT NULL CHECK(kind IN ('tomorrow','today')),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(operation_id,user_id,due_date,kind)
);
CREATE TABLE IF NOT EXISTS board_decisions (
 id bigserial PRIMARY KEY,meeting_date date NOT NULL,title text NOT NULL,decision text NOT NULL DEFAULT '',
 status text NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted','implemented','cancelled')),
 created_by bigint NOT NULL REFERENCES platform_users(id),created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS action_approvals (
 id uuid PRIMARY KEY,user_id bigint NOT NULL REFERENCES platform_users(id),job_id uuid,
 kind text NOT NULL,preview text NOT NULL,payload text NOT NULL,status text NOT NULL DEFAULT 'pending',
 expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),executed_at timestamptz,result text
);
CREATE TABLE IF NOT EXISTS audit_events (
 id bigserial PRIMARY KEY,user_id bigint,action text NOT NULL,target text,detail jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS link_tokens (hash text PRIMARY KEY,user_id bigint NOT NULL REFERENCES platform_users(id),expires_at timestamptz NOT NULL,used_at timestamptz);
CREATE TABLE IF NOT EXISTS telegram_pair_codes (code text PRIMARY KEY,telegram_id bigint NOT NULL,expires_at timestamptz NOT NULL,used_at timestamptz,created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS telegram_pair_codes_telegram_idx ON telegram_pair_codes(telegram_id);
CREATE TABLE IF NOT EXISTS accepted_commands (id bigint PRIMARY KEY,response text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS deletion_tombstones (user_id bigint NOT NULL,deleted_at timestamptz NOT NULL DEFAULT now(),kind text NOT NULL,target text NOT NULL);
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS google_access boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS invitations (email text PRIMARY KEY,role text NOT NULL DEFAULT 'member',created_by bigint NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE invitations ADD COLUMN IF NOT EXISTS email_status text NOT NULL DEFAULT 'pending';
ALTER TABLE invitations ADD COLUMN IF NOT EXISTS email_sent_at timestamptz;
ALTER TABLE invitations ADD COLUMN IF NOT EXISTS email_error text;
CREATE TABLE IF NOT EXISTS telegram_conversations(user_id bigint PRIMARY KEY REFERENCES platform_users(id),conversation_id uuid REFERENCES conversations(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS notifications(id bigserial PRIMARY KEY,user_id bigint REFERENCES platform_users(id),body text NOT NULL,status text NOT NULL DEFAULT 'pending',created_at timestamptz NOT NULL DEFAULT now());

ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS workspace_epoch int NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS telegram_preferences(user_id bigint PRIMARY KEY REFERENCES platform_users(id),model text NOT NULL DEFAULT 'inovens-combo-fast');
ALTER TABLE telegram_preferences ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'personal' CHECK(scope IN ('personal','board'));
ALTER TABLE documents ADD COLUMN IF NOT EXISTS generated boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS incidents (
 id uuid PRIMARY KEY,fingerprint text NOT NULL UNIQUE,source text NOT NULL,severity text NOT NULL DEFAULT 'error',
 code text NOT NULL,summary text NOT NULL,detail jsonb NOT NULL DEFAULT '{}',status text NOT NULL DEFAULT 'open',
 related_job uuid REFERENCES jobs(id) ON DELETE SET NULL,occurrences int NOT NULL DEFAULT 1,
 first_seen timestamptz NOT NULL DEFAULT now(),last_seen timestamptz NOT NULL DEFAULT now(),resolved_at timestamptz
);
CREATE INDEX IF NOT EXISTS incidents_status_seen ON incidents(status,last_seen DESC);
CREATE TABLE IF NOT EXISTS repair_runs (
 id uuid PRIMARY KEY,incident_id uuid NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,requested_by bigint REFERENCES platform_users(id),
 model text NOT NULL DEFAULT 'inovens-panel-fixer',status text NOT NULL DEFAULT 'queued',plan jsonb NOT NULL DEFAULT '{}',
 evidence jsonb NOT NULL DEFAULT '{}',error text,created_at timestamptz NOT NULL DEFAULT now(),started_at timestamptz,finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS repair_runs_incident ON repair_runs(incident_id,created_at DESC);
CREATE TABLE IF NOT EXISTS job_recovery (
 job_id uuid PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,prompt text NOT NULL,expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours'
);

-- Additive archive migration: existing documents and conversations remain intact.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS archive_meta jsonb NOT NULL DEFAULT '{}';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS archive_kind text NOT NULL DEFAULT 'general';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS review_status text NOT NULL DEFAULT 'ready';
CREATE INDEX IF NOT EXISTS documents_archive_meta ON documents USING gin(archive_meta);
CREATE INDEX IF NOT EXISTS documents_scope_created ON documents(scope,created_at DESC);
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS telegram_consent_version text;
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS telegram_consent_at timestamptz;
DO $$ BEGIN
 ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_scope_check;
 ALTER TABLE conversations ADD CONSTRAINT conversations_scope_check CHECK(scope IN ('personal','community','board','google'));
 ALTER TABLE telegram_preferences DROP CONSTRAINT IF EXISTS telegram_preferences_scope_check;
 ALTER TABLE telegram_preferences ADD CONSTRAINT telegram_preferences_scope_check CHECK(scope IN ('personal','community','board'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS document_shares (
 id uuid PRIMARY KEY,document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 from_user bigint NOT NULL REFERENCES platform_users(id),to_user bigint NOT NULL REFERENCES platform_users(id),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','declined','revoked')),
 introduction text,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(document_id,to_user),CHECK(from_user<>to_user)
);
CREATE INDEX IF NOT EXISTS document_shares_inbox ON document_shares(to_user,status,created_at DESC);
CREATE TABLE IF NOT EXISTS share_messages (
 id bigserial PRIMARY KEY,share_id uuid NOT NULL REFERENCES document_shares(id) ON DELETE CASCADE,
 sender_id bigint NOT NULL REFERENCES platform_users(id),body text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),read_at timestamptz
);
CREATE INDEX IF NOT EXISTS share_messages_thread ON share_messages(share_id,id DESC);
CREATE TABLE IF NOT EXISTS bot_memory (
 id bigserial PRIMARY KEY,title text NOT NULL,body text NOT NULL,active boolean NOT NULL DEFAULT true,
 created_by bigint NOT NULL REFERENCES platform_users(id),created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS memory_reminders (
 document_id uuid PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
 user_id bigint NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
 due_at timestamptz NOT NULL,sent_at timestamptz,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_reminders_due ON memory_reminders(due_at) WHERE sent_at IS NULL;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS last_error text;
CREATE TABLE IF NOT EXISTS telegram_update_failures (
 update_id bigint PRIMARY KEY,attempts int NOT NULL DEFAULT 1,last_error text NOT NULL,updated_at timestamptz NOT NULL DEFAULT now()
);
