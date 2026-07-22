create table if not exists employees (
  employee_id text primary key,
  name text not null,
  department text not null,
  office text not null,
  password text not null,
  username text,
  shift_start text default '09:00',
  shift_end text default '18:00',
  allowed_ips text
);

create table if not exists offices (
  office_id text primary key,
  office_name text not null,
  city text not null
);

create table if not exists attendance (
  record_id text primary key,
  employee_id text not null,
  employee_name text not null,
  department text not null,
  office text not null,
  date text not null,
  clock_in_time text not null,
  clock_out_time text,
  total_hours text,
  late_minutes text,
  extra_hours text,
  clock_in_ip text,
  clock_out_ip text,
  status text not null,
  late text not null,
  total_break_time text default '00:00:00',
  total_working_time text default '00:00:00',
  net_working_time text default '00:00:00',
  break_count text default '0',
  on_break text default 'No',
  current_break_id text,
  early_departure_minutes text default '0'
);

create table if not exists breaks (
  break_id text primary key,
  attendance_id text not null references attendance(record_id) on delete cascade,
  employee_id text not null,
  date text not null,
  break_start text not null,
  break_end text,
  duration_seconds text default '0',
  duration_display text default '00:00:00',
  break_start_ip text,
  break_end_ip text
);

alter table breaks add column if not exists break_start_ip text;
alter table breaks add column if not exists break_end_ip text;
alter table employees add column if not exists username text;
alter table employees add column if not exists shift_start text default '09:00';
alter table employees add column if not exists shift_end text default '18:00';
alter table employees add column if not exists allowed_ips text;
alter table attendance add column if not exists early_departure_minutes text default '0';

create table if not exists login_logs (
  timestamp text primary key,
  employee_name text,
  employee_id text,
  user_id text not null,
  role text not null,
  activity_type text,
  date text,
  time text,
  detected_ip text not null,
  isp text,
  browser text,
  operating_system text,
  device text,
  status text not null default 'success'
);

alter table login_logs add column if not exists employee_name text;
alter table login_logs add column if not exists employee_id text;
alter table login_logs add column if not exists activity_type text;
alter table login_logs add column if not exists date text;
alter table login_logs add column if not exists time text;
alter table login_logs add column if not exists isp text;
alter table login_logs add column if not exists browser text;
alter table login_logs add column if not exists operating_system text;
alter table login_logs add column if not exists device text;

create table if not exists denied_attempts (
  timestamp text primary key,
  employee_id text not null,
  action text not null,
  detected_ip text not null,
  reason text not null
);
