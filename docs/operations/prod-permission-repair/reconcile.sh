: reconcile-prod
# READ-ONLY production reconcile (stat / find / namei / test / systemctl show / curl / the audit itself). Writes nothing.
A=/opt/hyperchat-prod; SVC=hyperchat-prod
s() { echo; echo "## $*"; }
s "identity"
id "$SVC"; getent group "$SVC"
s "consumers of $A (units, nginx, cron) and the user each runs as"
for f in $(grep -lE "/opt/hyperchat-prod(/|$|[[:space:]])" /etc/systemd/system/*.service /etc/systemd/system/*.timer /lib/systemd/system/*.service 2>/dev/null); do
  echo "$f User=$(grep -E '^User=' "$f" | tail -1 | cut -d= -f2) Caps=$(grep -E '^CapabilityBoundingSet=' "$f" | tail -1 | cut -d= -f2-)"
done
grep -rlE "/opt/hyperchat-prod(/|;|[[:space:]]|$)" /etc/nginx /etc/cron* /var/spool/cron 2>/dev/null; echo "(end of nginx/cron references)"
grep -nE "opt/hyperchat|restore_check|python" /usr/local/sbin/hyperchat-pgbackup 2>&1 | head -10; echo "(end of pgbackup script references)"
s "top-level directories"
stat -c '%U:%G %a %F %n' /opt $A $A/releases $A/current
ls -ld $A $A/releases; lsattr -d $A $A/releases 2>&1
echo "current -> $(readlink -f $A/current)"
ls -1 $A/releases
s "violations (check_isolation.sh strict predicate), full list: owner:group mode type path"
V="$(find $A ! -type l \( ! -user root -o -perm /0022 -o -perm /0007 \) -printf '%u:%g %m %y %p\n' | LC_ALL=C sort -k4)"; echo "find exit=$?"
v() { printf '%s\n' "$V"; }
v
echo "total=$(v | grep -c .) pycache=$(v | grep -c '/__pycache__$')"
echo "realpath differs:"; v | while read -r _ _ _ p; do [ -z "$p" ] || [ "$(readlink -f "$p")" = "$p" ] || echo "  $p"; done; echo "(end)"
echo "sha256 of the path list: $(v | awk '{print $4}' | sha256sum | cut -c1-64)"
s "contents of the violating directories"
v | awk '{print $4}' | while read -r p; do find "$p" -mindepth 1 -maxdepth 1 -printf '%u:%g %m %y\n'; done | sort | uniq -c
echo "nested directories: $(v | awk '{print $4}' | while read -r p; do find "$p" -mindepth 1 -type d; done | wc -l)"
v | awk '{print $4}' | xargs -r ls -ld; v | awk '{print $4}' | xargs -r lsattr -d
s "parents of the violating directories"
v | awk '{print $4}' | xargs -r -n1 dirname | xargs -r stat -c '%U:%G %a %n'
s "running production services and timers"
for u in hyperchat-prod-api hyperchat-prod-worker hyperchat-prod-cleanup.timer hyperchat-pgbackup.timer hyperchat-offsite.timer; do
  echo "$u: $(systemctl show "$u" -p LoadState -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecMainStartTimestamp 2>&1 | tr '\n' ' ')"
done
for u in hyperchat-prod-api hyperchat-prod-worker; do
  pid="$(systemctl show -p MainPID --value "$u")"
  [ "$pid" -gt 0 ] 2>/dev/null && echo "$u pid $pid user=$(ps -o user= -p "$pid") $(grep -E '^(Gid|Groups):' /proc/$pid/status | tr '\n' ' ' | tr -s '\t ' ' ')"
done
s "health"
echo "prod: $(curl -fsS --max-time 5 http://127.0.0.1:8001/health)"
echo "dev:  $(curl -fsS --max-time 5 http://127.0.0.1:8000/health)"
s "service user access to the active release now"
R="$(readlink -f $A/current)"
namei -l "$R/backend/app/main.py"
for t in "-x $A" "-x $A/releases" "-r $R/backend/app/main.py" "-x $R/venv/bin/python"; do
  sudo -u "$SVC" test $t && echo "$SVC test $t: yes" || echo "$SVC test $t: NO"
done
s "current production audit (staged c2fe883 copy, read-only)"
bash /root/hyperchat-c2fe883/deploy/check_isolation.sh prod; echo "exit=$?"
s "DEV audit now (read-only)"
bash /root/hyperchat-c2fe883/deploy/check_isolation.sh dev | grep -c '^OK'; echo "exit=${PIPESTATUS[0]}"
