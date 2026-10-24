# Local WSL test of apply.sh / rollback.sh on a mock /opt/hyperchat built from the manifest (throwaway; root).
D="$(cd "$(dirname "$0")" && pwd)"  # this directory
B=/var/tmp/pfbin; pass=0; failn=0
t() { if [ "$1" = 0 ]; then pass=$((pass+1)); echo "PASS $2"; else failn=$((failn+1)); echo "FAIL $2"; fi; }
getent group hyperchat >/dev/null || groupadd -r hyperchat
id hyperchat >/dev/null 2>&1 || useradd -r -g hyperchat -s /usr/sbin/nologin hyperchat
MOCKS="/opt/hyperchat /opt/hyperchat-prod /root/hyperchat-c2fe883 /root/rehearsal-dev /etc/hyperchat /etc/postgresql/15/restoretest /etc/postgresql/17/hyperchat /etc/systemd/system/hyperchat-api.service /etc/systemd/system/hyperchat-worker.service /etc/systemd/system/hyperchat-cleanup.service /etc/systemd/system/hyperchat-dev-offsite.service"
for x in $MOCKS; do [ ! -e $x ] || { echo "refusing: $x exists"; exit 1; }; done
mkdir -p $B
# mock invariants: production tree, both clusters' client-auth files, DEV env file, the three DEV units
mkdir -p /opt/hyperchat-prod/releases/940137070657 /etc/hyperchat /etc/postgresql/15/restoretest /etc/postgresql/17/hyperchat /etc/systemd/system
ln -s /opt/hyperchat-prod/releases/940137070657 /opt/hyperchat-prod/current
for f in /etc/postgresql/15/restoretest/pg_hba.conf /etc/postgresql/15/restoretest/pg_ident.conf /etc/postgresql/17/hyperchat/pg_hba.conf /etc/postgresql/17/hyperchat/pg_ident.conf; do echo "# mock" > $f; done
echo X=1 > /etc/hyperchat/api.env
for u in api worker cleanup; do printf '[Service]
User=hyperchat
ExecStart=/opt/hyperchat/current/venv/bin/python
' > /etc/systemd/system/hyperchat-$u.service; done
build() {
  rm -rf /opt/hyperchat /root/rehearsal-dev; mkdir -m 700 /root/rehearsal-dev
  mkdir -p /opt/hyperchat/releases; chmod 755 /opt/hyperchat /opt/hyperchat/releases
  grep /__pycache__ $D/manifest.txt | cut -d/ -f5 | sort -u | while read -r r; do
    R=/opt/hyperchat/releases/$r; mkdir -p $R/backend/app $R/venv/bin $R/database/migrations/versions
    grep "^$R/" $D/manifest.txt | xargs -d '\n' -n1 dirname | xargs -d '\n' mkdir -p
    echo x > $R/backend/app/main.py; printf '#!/bin/sh\n' > $R/venv/bin/python; chmod 755 $R/venv/bin/python
    chgrp -R hyperchat $R; chmod -R g+rX,g-w,o-rwx $R
  done
  ln -s /opt/hyperchat/releases/229fe42381e2 /opt/hyperchat/current
  grep /__pycache__ $D/manifest.txt | while read -r p; do mkdir -p "$p"; chmod 755 "$p"; echo b > "$p/m.pyc"; chmod 640 "$p/m.pyc"; done
}
pkill -u hyperchat sleep 2>/dev/null; sudo -u hyperchat sleep 900 & sleep 1; P1=$(pgrep -u hyperchat sleep | head -1)
cat > $B/systemctl <<EOF
#!/bin/bash
case "\$*" in *--value*) echo $P1; echo $P1 ;; *) echo "MainPID=$P1"; echo NRestarts=0; echo ActiveState=active ;; esac
EOF
cat > $B/curl <<'EOF'
#!/bin/bash
[ -e /var/tmp/pfbin/unhealthy ] && [ "$(stat -c %a /opt/hyperchat/releases)" = 750 ] && exit 22
echo '{"status":"ok","release":"229fe42381e2","database":{"status":"ok"}}'
EOF
cat > $B/chmod <<'EOF'
#!/bin/bash
[ -e /var/tmp/pfbin/failon ] && [ "${@: -1}" = "$(cat /var/tmp/pfbin/failon)" ] && exit 1
[ -e /var/tmp/pfbin/touchhba ] && [ "${@: -1}" = /opt/hyperchat/releases ] && echo "# changed" >> /etc/postgresql/15/restoretest/pg_hba.conf
exec /usr/bin/chmod "$@"
EOF
/usr/bin/chmod +x $B/systemctl $B/curl $B/chmod
mkdir -p /root/hyperchat-c2fe883/deploy
cat > /root/hyperchat-c2fe883/deploy/check_isolation.sh <<EOF
source <(sed -n '/^audit_expected()/,/^}/p' $D/apply.sh)
v="\$(find /opt/hyperchat ! -type l \( ! -user root -o -perm /0022 -o -perm /0007 \) -print | head -5 | tr '\n' ' ')"
audit_expected dev 2>/dev/null | { [ -z "\$v" ] && cat || sed "1s|.*|FAIL  release tree: \$v|"; }; exit 1
EOF
export PATH=$B:$PATH
run() { bash "$D/$1" 2>&1; }

build; out="$(run apply.sh)"; echo "$out" | sed 's/^/  | /'
echo "$out" | grep -q '^verify OK' ; t $? "happy path: apply ends verify OK"
[ "$(find /opt/hyperchat ! -type l \( ! -user root -o -perm /0022 -o -perm /0007 \) | wc -l)" = 0 ]; t $? "no violation remains"
[ "$(xargs -d '\n' stat -c '%U:%G %a' < $D/manifest.txt | sort -u)" = "root:hyperchat 750" ]; t $? "all 117 root:hyperchat 750"
[ "$(find /opt/hyperchat -name m.pyc -printf '%u:%g %m\n' | sort -u)" = "root:root 640" ]; t $? ".pyc files untouched"
[ "$(stat -c '%U:%G %a' /opt/hyperchat/releases/229fe42381e2/backend/app/main.py)" = "root:hyperchat 640" ]; t $? "release files untouched"
sudo -u hyperchat test -r /opt/hyperchat/current/backend/app/main.py; t $? "service user reads its release after"
sudo -u nobody test -x /opt/hyperchat; [ $? != 0 ]; t $? "other has no access after"
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: /root/rehearsal-dev/permfix exists'; t $? "second apply refused (single-use)"
out="$(run rollback.sh)"; echo "$out" | grep -q '^rollback OK'; t $? "rollback OK"
[ "$(xargs -d '\n' stat -c '%U:%G %a' < $D/manifest.txt | sort -u)" = "root:root 755" ]; t $? "rollback restores root:root 755 exactly"
[ "$(find /opt/hyperchat -name m.pyc -printf '%u:%g %m\n' | sort -u)" = "root:root 640" ]; t $? "rollback leaves .pyc untouched"
out="$(run rollback.sh)"; echo "$out" | grep -q '^rollback OK'; t $? "rollback re-runnable"

build; mkdir -m 755 /opt/hyperchat/releases/229fe42381e2/backend/app/new/__pycache__ -p
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: live violations differ'; t $? "drift (new __pycache__) stops pf_pre"
[ "$(stat -c '%G %a' /opt/hyperchat)" = "root 755" ]; t $? "drift: nothing changed"

build; p=$(sed -n 50p $D/manifest.txt); rm -rf "$p"; mkdir -p /var/tmp/elsewhere; chmod 755 /var/tmp/elsewhere; ln -s /var/tmp/elsewhere "$p"
out="$(run apply.sh)"; echo "$out" | grep -qE 'STOP: (live violations differ|.* is not a plain directory)'; t $? "symlinked manifest path stops pf_pre"
[ "$(stat -c '%G %a' /var/tmp/elsewhere)" = "root 755" ]; t $? "symlink target untouched"

build; touch $B/unhealthy
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: hyperchat lost access or API unhealthy after chmod /opt/hyperchat/releases'; t $? "unhealthy after chmod stops pf_top"
[ "$(stat -c %a /opt/hyperchat/releases)" = 755 ]; t $? "auto-revert of the mode"
[ "$(stat -c '%U:%G %a' "$(sed -n 3p $D/manifest.txt)")" = "root:root 755" ]; t $? "no __pycache__ changed after pf_top STOP"
rm -f $B/unhealthy

build; chmod 700 "$(sed -n 3p $D/manifest.txt)"
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: live violations differ\|STOP: .* is root:root 700'; t $? "changed mode stops pf_pre"

build; P50=$(grep /__pycache__ $D/manifest.txt | sed -n 50p); echo "$P50" > $B/failon
out="$(run apply.sh)"; echo "$out" | grep -q "STOP: change failed on $P50 (49 done)"; t $? "partial failure at path 50 stops pf_pycache"
[ "$(xargs -d '
' stat -c '%G %a' < $D/manifest.txt | sort | uniq -c | tr -s ' ' | sed 's/^ //' | tr '
' ,)" = "51 hyperchat 750,1 hyperchat 755,65 root 755," ]; t $? "partial state: 51 changed, path 50 group-only, 65 untouched"
rm -f $B/failon; out="$(run rollback.sh)"; echo "$out" | grep -q '^rollback OK'; t $? "rollback after partial failure OK"
[ "$(xargs -d '
' stat -c '%U:%G %a' < $D/manifest.txt | sort -u)" = "root:root 755" ]; t $? "partial rollback restores all 117 exactly"

build; touch $B/touchhba
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: the release symlink, production, PostgreSQL config or a unit changed'; t $? "a changed invariant (pg_hba) stops pf_verify"
rm -f $B/touchhba; echo "# mock" > /etc/postgresql/15/restoretest/pg_hba.conf

build; printf '[Service]
User=root
ExecStart=/opt/hyperchat/current/venv/bin/python
' > /etc/systemd/system/hyperchat-dev-offsite.service
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: unexpected consumers'; t $? "an unexpected consumer unit stops pf_pre"
[ "$(stat -c '%G %a' /opt/hyperchat)" = "root 755" ]; t $? "unexpected consumer: nothing changed"
rm -f /etc/systemd/system/hyperchat-dev-offsite.service

build; rm /opt/hyperchat/current; ln -s /opt/hyperchat/releases/e953eab498f6 /opt/hyperchat/current
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: current is not release 229fe42381e2'; t $? "another active release stops pf_pre"

pkill -u hyperchat sleep; rm -rf $MOCKS /var/tmp/elsewhere $B
echo "passed=$pass failed=$failn"
