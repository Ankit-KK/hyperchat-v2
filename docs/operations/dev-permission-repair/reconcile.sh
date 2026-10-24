: reconcile
# READ-ONLY DEV reconcile (stat / find / namei / test / systemctl show / curl / the audit itself). Writes nothing.
A=/opt/hyperchat
s() { echo; echo "## $*"; }
s "identity"
id hyperchat; getent group hyperchat
s "consumers of $A (units, nginx, cron) and the user each runs as"
for f in $(grep -lE "/opt/hyperchat(/|$|[[:space:]])" /etc/systemd/system/*.service /etc/systemd/system/*.timer /lib/systemd/system/*.service 2>/dev/null); do
  echo "$f User=$(grep -E '^User=' "$f" | tail -1 | cut -d= -f2) Group=$(grep -E '^Group=' "$f" | tail -1 | cut -d= -f2) Caps=$(grep -E '^CapabilityBoundingSet=' "$f" | tail -1 | cut -d= -f2-)"
done
grep -rnE "/opt/hyperchat(/|;|[[:space:]]|$)" /etc/nginx /etc/cron* /var/spool/cron 2>/dev/null | head -20; echo "(end of nginx/cron references)"
s "top-level directories"
stat -c '%U:%G %a %F %n' / /opt $A $A/releases $A/current
ls -ld $A $A/releases; lsattr -d $A $A/releases 2>&1
echo "current -> $(readlink -f $A/current)"
s "violations (check_isolation.sh strict predicate), full list: owner:group mode type path"
V="$(find $A ! -type l \( ! -user root -o -perm /0022 -o -perm /0007 \) -printf '%u:%g %m %y %p\n' | LC_ALL=C sort -k4)"
v() { printf '%s\n' "$V"; }
v
echo "total=$(wc -l < <(v)) dirs=$(awk '$3=="d"' <(v) | wc -l) nondirs=$(awk '$3!="d"' <(v) | wc -l)"
echo "pycache=$(grep -c '/__pycache__$' <(v)) other=$(grep -vc '/__pycache__$' <(v))"
echo "by owner:group mode:"; awk '{print $1, $2}' <(v) | sort | uniq -c
echo "by release:"; awk '{n=split($4,a,"/"); print (a[4]=="releases" && n>=5) ? a[5] : $4}' <(v) | sort | uniq -c
echo "realpath differs (symlink on the way):"; while read -r _ _ _ p; do [ "$(readlink -f "$p")" = "$p" ] || echo "  $p"; done < <(v); echo "(end)"
echo "sha256 of the path list: $(awk '{print $4}' <(v) | sha256sum | cut -c1-64)"
s "contents of the violating __pycache__ directories (entries by owner:group mode type)"
grep '/__pycache__$' <(v) | awk '{print $4}' | while read -r p; do find "$p" -mindepth 1 -maxdepth 1 -printf '%u:%g %m %y\n'; done | sort | uniq -c
echo "nested directories inside them: $(grep '/__pycache__$' <(v) | awk '{print $4}' | while read -r p; do find "$p" -mindepth 1 -type d; done | wc -l)"
echo "ACL marks (+) on violating paths: $(awk '{print $4}' <(v) | xargs -r ls -ld | awk 'substr($1,11,1)=="+"' | wc -l)"
s "running DEV services"
for u in hyperchat-api hyperchat-worker hyperchat-cleanup.timer hyperchat-dev-offsite.timer hyperchat-dev-pgbackup.timer; do
  echo "$u: $(systemctl show "$u" -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecMainStartTimestamp 2>&1 | tr '\n' ' ')"
done
for u in hyperchat-api hyperchat-worker; do
  pid="$(systemctl show -p MainPID --value "$u")"
  [ "$pid" -gt 0 ] 2>/dev/null && echo "$u pid $pid user=$(ps -o user= -p "$pid") $(grep -E '^(Gid|Groups):' /proc/$pid/status | tr '\n' ' ' | tr -s '\t ' ' ')"
done
s "health"
curl -fsS --max-time 5 http://127.0.0.1:8000/health; echo
s "service user access to the active release now"
R="$(readlink -f $A/current)"
namei -l "$R/backend/app/main.py"
for t in "-x $A" "-x $A/releases" "-r $R/backend/app/main.py" "-x $R/venv/bin/python"; do
  sudo -u hyperchat test $t && echo "hyperchat test $t: yes" || echo "hyperchat test $t: NO"
done
s "current DEV audit (staged c2fe883 copy, read-only)"
bash /root/hyperchat-c2fe883/deploy/check_isolation.sh dev; echo "exit=$?"
