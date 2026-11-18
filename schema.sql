begin;

create schema if not exists workout_private;
revoke all on schema workout_private from public;
create table if not exists workout_private.owner (
  singleton boolean primary key default true check (singleton),
  user_id uuid not null unique references auth.users(id) on delete cascade
);
revoke all on workout_private.owner from public, anon, authenticated;

create or replace function public.workout_owner_allowed()
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (select 1 from workout_private.owner where user_id = (select auth.uid()));
$$;
revoke all on function public.workout_owner_allowed() from public, anon;
grant execute on function public.workout_owner_allowed() to authenticated;

create table if not exists public.workout_routines (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  routine_key text not null,
  name text not null check (length(name) between 1 and 100),
  weekday smallint not null check (weekday between 1 and 7),
  exercise_plan jsonb not null check (jsonb_typeof(exercise_plan) = 'array' and jsonb_array_length(exercise_plan) between 1 and 60),
  unique (user_id, routine_key),
  unique (id, user_id)
);

create table if not exists public.workout_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  routine_id uuid not null,
  routine_name text not null,
  exercise_plan jsonb not null,
  started_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz check (finished_at >= started_at),
  pending_exercise integer,
  pending_set integer,
  pending_since timestamptz,
  unique (id, user_id),
  foreign key (routine_id, user_id) references public.workout_routines(id, user_id)
);
create unique index if not exists workout_one_open_session on public.workout_sessions(user_id) where finished_at is null;
create index if not exists workout_sessions_date on public.workout_sessions(user_id, started_at desc);

create table if not exists public.workout_sets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  session_id uuid not null,
  exercise_index integer not null check (exercise_index >= 0),
  exercise_name text not null,
  set_number integer not null check (set_number between 1 and 99),
  weight_kg numeric(8,3) check (weight_kg >= 0 and weight_kg <= 5000),
  reps integer check (reps between 0 and 1000),
  duration_seconds integer check (duration_seconds between 1 and 86400),
  logged_at timestamptz not null,
  set_started_at timestamptz,
  gap_seconds integer check (gap_seconds >= 0),
  rest_seconds integer check (rest_seconds >= 0),
  check (reps is not null or duration_seconds is not null),
  foreign key (session_id, user_id) references public.workout_sessions(id, user_id) on delete cascade,
  unique (session_id, exercise_index, set_number)
);
create index if not exists workout_sets_chronological on public.workout_sets(session_id, logged_at desc);
create index if not exists workout_sets_last_exercise on public.workout_sets(user_id, exercise_name, logged_at desc);

create table if not exists public.workout_cardio (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  fingerprint text not null,
  source text not null,
  activity text not null,
  started_at timestamptz not null,
  duration_seconds integer not null check (duration_seconds > 0),
  distance_km numeric check (distance_km >= 0),
  calories_kcal numeric check (calories_kcal >= 0),
  avg_hr_bpm numeric check (avg_hr_bpm > 0),
  max_hr_bpm numeric check (max_hr_bpm > 0),
  steps integer check (steps >= 0),
  details jsonb not null default '{}'::jsonb,
  unique (user_id, fingerprint)
);
create index if not exists workout_cardio_date on public.workout_cardio(user_id, started_at desc);

do $$
declare table_name text;
begin
  foreach table_name in array array['workout_routines','workout_sessions','workout_sets','workout_cardio'] loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('revoke all on table public.%I from public, anon, authenticated', table_name);
    execute format('grant select, insert, update on table public.%I to authenticated', table_name);
    execute format('drop policy if exists private_owner on public.%I', table_name);
    execute format('create policy private_owner on public.%I to authenticated using (user_id = (select auth.uid()) and (select public.workout_owner_allowed())) with check (user_id = (select auth.uid()) and (select public.workout_owner_allowed()))', table_name);
  end loop;
end;
$$;

create or replace function public.workout_start(p_routine uuid)
returns uuid language plpgsql set search_path = '' as $$
declare session_id uuid;
begin
  insert into public.workout_sessions(routine_id, routine_name, exercise_plan)
    select id, name, exercise_plan from public.workout_routines where id = p_routine
    on conflict (user_id) where finished_at is null do nothing
    returning id into session_id;
  if session_id is null then
    select id into session_id from public.workout_sessions where user_id = auth.uid() and finished_at is null;
  end if;
  if session_id is null then raise exception 'The routine could not be started.'; end if;
  return session_id;
end;
$$;

create or replace function public.workout_stop(p_session uuid)
returns void language plpgsql set search_path = '' as $$
begin
  update public.workout_sessions set finished_at = clock_timestamp(), pending_exercise = null,
    pending_set = null, pending_since = null
  where id = p_session and finished_at is null;
end;
$$;

create or replace function public.workout_begin_set(p_session uuid, p_exercise integer, p_set integer)
returns void language plpgsql set search_path = '' as $$
declare session_row public.workout_sessions%rowtype;
declare next_set integer;
begin
  select * into session_row from public.workout_sessions where id = p_session for update;
  if not found or session_row.finished_at is not null then raise exception 'The workout is closed.'; end if;
  if p_exercise is null or p_exercise < 0 or p_exercise >= jsonb_array_length(session_row.exercise_plan) then
    raise exception 'Choose an exercise in the workout.';
  end if;
  select coalesce(max(set_number), 0) + 1 into next_set from public.workout_sets
    where session_id = p_session and exercise_index = p_exercise;
  if p_set is distinct from next_set or next_set > 99 then raise exception 'Refresh to see the next set.'; end if;
  if session_row.pending_since is not null then
    if session_row.pending_exercise = p_exercise and session_row.pending_set = p_set then return; end if;
    raise exception 'Finish or clear the set already in progress.';
  end if;
  update public.workout_sessions set pending_exercise = p_exercise, pending_set = p_set,
    pending_since = clock_timestamp() where id = p_session;
end;
$$;

create or replace function public.workout_clear_set(p_session uuid, p_since timestamptz)
returns void language sql set search_path = '' as $$
  update public.workout_sessions set pending_exercise = null, pending_set = null, pending_since = null
  where id = p_session and finished_at is null and pending_since = p_since;
$$;

create or replace function public.workout_log_set(
  p_session uuid, p_exercise integer, p_set integer, p_request uuid,
  p_weight numeric, p_reps integer, p_duration integer
)
returns void language plpgsql set search_path = '' as $$
declare session_row public.workout_sessions%rowtype;
declare previous_log timestamptz;
declare set_start timestamptz;
declare recorded timestamptz;
declare next_set integer;
begin
  select * into session_row from public.workout_sessions where id = p_session for update;
  if not found then raise exception 'The workout could not be found.'; end if;
  if exists (select 1 from public.workout_sets where id = p_request and session_id = p_session) then return; end if;
  if session_row.finished_at is not null then raise exception 'The workout is closed.'; end if;
  if p_exercise is null or p_exercise < 0 or p_exercise >= jsonb_array_length(session_row.exercise_plan) then
    raise exception 'Choose an exercise in the workout.';
  end if;
  select coalesce(max(set_number), 0) + 1 into next_set from public.workout_sets
    where session_id = p_session and exercise_index = p_exercise;
  if p_set is distinct from next_set or next_set > 99 then raise exception 'This set changed. Refresh to see the next set.'; end if;
  if session_row.pending_since is not null then
    if session_row.pending_exercise <> p_exercise or session_row.pending_set <> p_set then
      raise exception 'Finish or clear the set already in progress.';
    end if;
    set_start := session_row.pending_since;
  end if;
  select max(logged_at) into previous_log from public.workout_sets where session_id = p_session;
  recorded := clock_timestamp();
  insert into public.workout_sets(id, session_id, exercise_index, exercise_name, set_number,
    weight_kg, reps, duration_seconds, logged_at, set_started_at, gap_seconds, rest_seconds)
  values (p_request, p_session, p_exercise, session_row.exercise_plan -> p_exercise ->> 'exercise', p_set,
    p_weight, p_reps, p_duration, recorded, set_start,
    case when previous_log is not null then greatest(0, floor(extract(epoch from recorded - previous_log)))::integer end,
    case when previous_log is not null and set_start is not null
      then greatest(0, floor(extract(epoch from set_start - previous_log)))::integer end);
  update public.workout_sessions set pending_exercise = null, pending_set = null, pending_since = null where id = p_session;
end;
$$;

revoke all on function public.workout_start(uuid), public.workout_stop(uuid), public.workout_begin_set(uuid,integer,integer),
  public.workout_clear_set(uuid,timestamptz), public.workout_log_set(uuid,integer,integer,uuid,numeric,integer,integer) from public, anon;
grant execute on function public.workout_start(uuid), public.workout_stop(uuid), public.workout_begin_set(uuid,integer,integer),
  public.workout_clear_set(uuid,timestamptz), public.workout_log_set(uuid,integer,integer,uuid,numeric,integer,integer) to authenticated;

commit;
