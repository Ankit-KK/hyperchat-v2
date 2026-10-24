# Local test of the production apply.sh / rollback.sh on a mock /opt/hyperchat-prod (and a mock DEV tree for the
# invariants), built from the manifest. Throwaway machine only (WSL), as root; refuses if any mocked path exists.
D="$(cd "$(dirname "$0")" && pwd)"  # this directory
B=/var/tmp/pfbin; pass=0; failn=0
t() { if [ "$1" = 0 ]; then pass=$((pass+1)); echo "PASS $2"; else failn=$((failn+1)); echo "FAIL $2"; fi; }
for u in hyperchat hyperchat-prod; do
  getent group $u >/dev/null || groupadd -r $u
  id $u >/dev/null 2>&1 || useradd -r -g $u -s /usr/sbin/nologin $u
done
MOCKS="/opt/hyperchat /opt/hyperchat-prod /root/hyperchat-c2fe883 /root/prod-permfix /etc/hyperchat /etc/hyperchat-prod /etc/postgresql/15/restoretest /etc/postgresql/17/hyperchat /usr/local/sbin/hyperchat-pgbackup /etc/systemd/system/hyperchat-api.service /etc/systemd/system/hyperchat-worker.service /etc/systemd/system/hyperchat-cleanup.service /etc/systemd/system/hyperchat-prod-api.service /etc/systemd/system/hyperchat-prod-cleanup.service /etc/systemd/system/hyperchat-offsite.service"
for x in $MOCKS $B; do [ ! -e $x ] || { echo "refusing: $x exists"; exit 1; }; done
mkdir -p $B /etc/hyperchat /etc/hyperchat-prod /etc/postgresql/15/restoretest /etc/postgresql/17/hyperchat /etc/systemd/system /usr/local/sbin
for f in /etc/postgresql/15/restoretest/pg_hba.conf /etc/postgresql/15/restoretest/pg_ident.conf /etc/postgresql/17/hyperchat/pg_hba.conf /etc/postgresql/17/hyperchat/pg_ident.conf /usr/local/sbin/hyperchat-pgbackup; do echo "# mock" > $f; done
echo X=1 > /etc/hyperchat/api.env; echo X=1 > /etc/hyperchat-prod/api.env; chgrp hyperchat-prod /etc/hyperchat-prod/api.env; chmod 640 /etc/hyperchat-prod/api.env
for u in api worker cleanup; do printf '[Service]\nUser=hyperchat\nExecStart=/opt/hyperchat/current/venv/bin/python\n' > /etc/systemd/system/hyperchat-$u.service; done
for u in api cleanup; do printf '[Service]\nUser=hyperchat-prod\nExecStart=/opt/hyperchat-prod/current/venv/bin/python\n' > /etc/systemd/system/hyperchat-prod-$u.service; done
# DEV, as left by the DEV repair (root:hyperchat 750); only checked, never changed
mkdir -p /opt/hyperchat/releases/229fe42381e2; chgrp hyperchat /opt/hyperchat /opt/hyperchat/releases /opt/hyperchat/releases/229fe42381e2
chmod 750 /opt/hyperchat /opt/hyperchat/releases /opt/hyperchat/releases/229fe42381e2; ln -s /opt/hyperchat/releases/229fe42381e2 /opt/hyperchat/current
devstate() { find /opt/hyperchat -printf '%u:%g %m %i %C@ %p\n' | sort; }
DEV0="$(devstate)"
build() {
  rm -rf /opt/hyperchat-prod /root/prod-permfix
  R=/opt/hyperchat-prod/releases/940137070657
  mkdir -p $R/backend/app $R/venv/bin; xargs -d '\n' -n1 dirname < $D/manifest.txt | xargs -d '\n' mkdir -p
  echo x > $R/backend/app/main.py; printf '#!/bin/sh\n' > $R/venv/bin/python; chmod 755 $R/venv/bin/python
  chgrp -R hyperchat-prod /opt/hyperchat-prod; chmod -R g+rX,g-w,o-rwx /opt/hyperchat-prod
  ln -s $R /opt/hyperchat-prod/current
  while read -r p; do mkdir "$p"; chmod 755 "$p"; echo b > "$p/m.pyc"; chmod 640 "$p/m.pyc"; done < $D/manifest.txt
}
pkill -u hyperchat-prod sleep 2>/dev/null; sudo -u hyperchat-prod sleep 1800 & sudo -u hyperchat sleep 1800 & sleep 1
P1=$(pgrep -u hyperchat-prod sleep | head -1); P2=$(pgrep -u hyperchat sleep | head -1)
cat > $B/systemctl <<EOF
#!/bin/bash
[ -e $B/sysfail ] && exit 1
for u in "\$@"; do case "\$u" in hyperchat-prod*) p=$P1 ;; hyperchat*) p=$P2 ;; *) continue ;; esac
  case "\$*" in *--value*) [ -e $B/nopid ] && echo 0 || echo \$p ;; *) printf "MainPID=\$p\nNRestarts=0\nActiveState=active\n\n" ;; esac; done
EOF
cat > $B/curl <<'EOF'
#!/bin/bash
case "$*" in
  *:8001/*) [ -e /var/tmp/pfbin/prodfail ] && exit 7
            [ -e /var/tmp/pfbin/prodfail_after ] && [ "$(stat -c %a "$(sed -n 3p /var/tmp/pfbin/manifest)")" = 750 ] && exit 22
            echo '{"status":"ok","release":"940137070657","database":{"status":"ok"}}' ;;
  *:8000/*) [ -e /var/tmp/pfbin/devfail ] && exit 7
            echo '{"status":"ok","release":"229fe42381e2","database":{"status":"ok"}}' ;;
esac
EOF
cp $D/manifest.txt $B/manifest
cat > $B/chmod <<'EOF'
#!/bin/bash
[ -e /var/tmp/pfbin/failon ] && [ "${@: -1}" = "$(cat /var/tmp/pfbin/failon)" ] && exit 1
[ -e /var/tmp/pfbin/touchhba ] && [ "${@: -1}" = "$(sed -n 2p /var/tmp/pfbin/manifest)" ] && echo "# changed" >> /etc/postgresql/15/restoretest/pg_hba.conf
[ -e /var/tmp/pfbin/devfail_after ] && [ "${@: -1}" = "$(sed -n 2p /var/tmp/pfbin/manifest)" ] && touch /var/tmp/pfbin/devfail
[ -e /var/tmp/pfbin/findfail_after ] && [ "${@: -1}" = "$(sed -n 5p /var/tmp/pfbin/manifest)" ] && touch /var/tmp/pfbin/findfail
exec /usr/bin/chmod "$@"
EOF
for c in find sort lsattr xargs; do
  printf '#!/bin/bash\n[ -e /var/tmp/pfbin/%sfail ] && exit 1\nexec %s "$@"\n' $c "$(command -v $c)" > $B/$c
done
/usr/bin/chmod +x $B/systemctl $B/curl $B/chmod $B/find $B/sort $B/lsattr $B/xargs
mkdir -p /root/hyperchat-c2fe883/deploy
# mock audit: the runbook's expected lines, with the release-tree line computed from the real strict predicate
cat > /root/hyperchat-c2fe883/deploy/check_isolation.sh <<EOF
[ -e $B/auditempty ] && exit 1
[ -e $B/auditrc0 ] && exit 0
source <(sed -n '/^audit_expected()/,/^}/p' $D/apply.sh)
case "\$1" in dev) app=/opt/hyperchat ;; prod) app=/opt/hyperchat-prod ;; esac
v="\$(find \$app ! -type l \( ! -user root -o -perm /0022 -o -perm /0007 \) -print | head -5 | tr '\n' ' ')"
audit_expected "\$1" 2>/dev/null | { [ -z "\$v" ] && cat || sed "1s|.*|FAIL  release tree: \$v|"; }; exit 1
EOF
export PATH=$B:$PATH
run() { bash "$D/$1" 2>&1; }
unchanged() { [ "$(xargs -d '\n' stat -c '%U:%G %a' < $D/manifest.txt | sort -u)" = "root:root 755" ]; }
violations_now() { find /opt/hyperchat-prod ! -type l \( ! -user root -o -perm /0022 -o -perm /0007 \) | wc -l; }

build; out="$(run apply.sh)"; echo "$out" | sed 's/^/  | /'
echo "$out" | grep -q '^verify OK' ; t $? "happy path: apply ends verify OK"
[ "$(violations_now)" = 0 ]; t $? "no violation remains"
[ "$(xargs -d '\n' stat -c '%U:%G %a' < $D/manifest.txt | sort -u)" = "root:hyperchat-prod 750" ]; t $? "all 5 root:hyperchat-prod 750"
[ "$(find /opt/hyperchat-prod -name m.pyc -printf '%u:%g %m\n' | sort -u)" = "root:root 640" ]; t $? ".pyc files untouched"
[ "$(stat -c '%U:%G %a' /opt/hyperchat-prod/releases/940137070657/backend/app/main.py)" = "root:hyperchat-prod 640" ]; t $? "release files untouched"
[ "$(stat -c '%U:%G %a' /opt/hyperchat-prod /opt/hyperchat-prod/releases | sort -u)" = "root:hyperchat-prod 750" ]; t $? "production top directories untouched"
sudo -u hyperchat-prod test -r /opt/hyperchat-prod/current/backend/app/main.py; t $? "service user reads its release after"
sudo -u nobody test -x "$(sed -n 1p $D/manifest.txt)"; [ $? != 0 ]; t $? "other has no access after"
[ "$(devstate)" = "$DEV0" ]; t $? "DEV tree untouched"
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: /root/prod-permfix exists'; t $? "second apply refused (single-use)"
out="$(run rollback.sh)"; echo "$out" | grep -q '^rollback OK'; t $? "rollback OK"
unchanged; t $? "rollback restores root:root 755 exactly"
[ "$(find /opt/hyperchat-prod -name m.pyc -printf '%u:%g %m\n' | sort -u)" = "root:root 640" ]; t $? "rollback leaves .pyc untouched"
out="$(run rollback.sh)"; echo "$out" | grep -q '^rollback OK'; t $? "rollback re-runnable"

build; mkdir -m 755 /opt/hyperchat-prod/releases/940137070657/backend/app/__pycache__/../new_pycache_parent -p; mkdir -m 755 /opt/hyperchat-prod/releases/940137070657/backend/app/new_pycache_parent/__pycache__; chgrp hyperchat-prod /opt/hyperchat-prod/releases/940137070657/backend/app/new_pycache_parent; chmod 750 /opt/hyperchat-prod/releases/940137070657/backend/app/new_pycache_parent
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: live violations differ'; t $? "drift (a 6th __pycache__) stops pf_pre"
unchanged; t $? "drift: nothing changed"

build; p=$(sed -n 3p $D/manifest.txt); rm -rf "$p"; mkdir -p /var/tmp/elsewhere; chmod 755 /var/tmp/elsewhere; ln -s /var/tmp/elsewhere "$p"
out="$(run apply.sh)"; echo "$out" | grep -qE 'STOP: (live violations differ|.* is not a plain directory)'; t $? "symlinked manifest path stops pf_pre"
[ "$(stat -c '%G %a' /var/tmp/elsewhere)" = "root 755" ]; t $? "symlink target untouched"

build; chmod 700 "$(sed -n 1p $D/manifest.txt)"
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: live violations differ'; t $? "changed mode stops pf_pre"

build; chmod 755 /opt/hyperchat-prod/releases
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: /opt/hyperchat-prod/releases is not root:hyperchat-prod 750'; t $? "unexpected top-directory mode stops pf_pre"

build; rm /opt/hyperchat-prod/current; ln -s /opt/hyperchat-prod/releases /opt/hyperchat-prod/current
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: current is not release 940137070657'; t $? "another active release stops pf_pre"

build; printf '[Service]\nExecStart=/opt/hyperchat-prod/current/venv/bin/python\n' > /etc/systemd/system/hyperchat-offsite.service
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: unexpected consumers'; t $? "an unexpected consumer unit stops pf_pre"
unchanged; t $? "unexpected consumer: nothing changed"; rm -f /etc/systemd/system/hyperchat-offsite.service

for f in prodfail devfail sysfail nopid findfail sortfail lsattrfail xargsfail; do
  case $f in
    prodfail) want='STOP: production API not healthy on 940137070657 before the change' ;;
    devfail) want='STOP: cannot collect the invariants' ;;
    sysfail) want='STOP: cannot collect the invariants' ;;
    nopid) want='STOP: cannot read the production API main PID' ;;
    findfail|sortfail) want='STOP: the violations scan failed (find or sort)' ;;
    lsattrfail) want='STOP: cannot read the attributes' ;;
    xargsfail) want='STOP: cannot record the before-state' ;;
  esac
  build; touch $B/$f; out="$(run apply.sh)"; rm -f $B/$f
  echo "$out" | grep -qF "$want"; t $? "$f stops pf_pre"
  unchanged; t $? "$f: no permission changed"
done

build; mv /etc/postgresql/17/hyperchat/pg_ident.conf /var/tmp/pg_ident.saved
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: cannot collect the invariants'; t $? "failed sha256sum (missing pg_ident) stops pf_pre"
unchanged; t $? "failed sha256sum: no permission changed"; mv /var/tmp/pg_ident.saved /etc/postgresql/17/hyperchat/pg_ident.conf

build; rm /opt/hyperchat/current
out="$(run apply.sh)"; echo "$out" | grep -q 'STOP: cannot collect the invariants'; t $? "failed DEV metadata (no DEV symlink) stops pf_pre"
unchanged; t $? "failed DEV metadata: no permission changed"; ln -s /opt/hyperchat/releases/229fe42381e2 /opt/hyperchat/current; DEV0="$(devstate)"

build; P3=$(sed -n 3p $D/manifest.txt); echo "$P3" > $B/failon
out="$(run apply.sh)"; rm -f $B/failon
echo "$out" | grep -qF "STOP: change failed on $P3 (2 done)"; t $? "partial failure at path 3 stops pf_pycache"
[ "$(xargs -d '\n' stat -c '%G %a' < $D/manifest.txt | tr '\n' ,)" = "hyperchat-prod 750,hyperchat-prod 750,hyperchat-prod 755,root 755,root 755," ]; t $? "partial state: 2 changed, path 3 group-only, 2 untouched"
out="$(run rollback.sh)"; echo "$out" | grep -q '^rollback OK'; t $? "rollback after partial failure OK"
unchanged; t $? "partial rollback restores all 5 exactly"

build; touch $B/prodfail_after
out="$(run apply.sh)"; rm -f $B/prodfail_after
echo "$out" | grep -qF 'STOP: production API not healthy on 940137070657'; t $? "production unhealthy after the change stops pf_verify"
echo "$out" | grep -q '^verify OK'; [ $? != 0 ]; t $? "production unhealthy after: no verify OK"

build; touch $B/devfail_after
out="$(run apply.sh)"; rm -f $B/devfail_after $B/devfail
echo "$out" | grep -qF 'STOP: cannot collect the invariants after the change'; t $? "DEV unhealthy after the change stops pf_verify"

build; touch $B/touchhba
out="$(run apply.sh)"; rm -f $B/touchhba; echo "# mock" > /etc/postgresql/15/restoretest/pg_hba.conf
echo "$out" | grep -qF 'STOP: a release symlink, DEV, the PostgreSQL config, a unit or the backup script changed'; t $? "a changed invariant (pg_hba) stops pf_verify"

build; touch $B/findfail_after
out="$(run apply.sh)"; rm -f $B/findfail_after $B/findfail
echo "$out" | grep -qF 'STOP: the violations scan failed after the change (find or sort)'; t $? "failed find after the change (empty stdout) stops pf_verify"
echo "$out" | grep -q '^verify OK'; [ $? != 0 ]; t $? "failed find after: no verify OK"

build; touch $B/auditempty
out="$(run apply.sh)"; rm -f $B/auditempty
echo "$out" | grep -qF 'STOP: prod audit differs from the expected lines'; t $? "audit exit 1 with empty stdout after the change stops pf_verify"

build; touch $B/auditrc0
out="$(run apply.sh)"; rm -f $B/auditrc0
echo "$out" | grep -qF 'STOP: prod audit exited 0 or wrote to stderr'; t $? "audit exit 0 with empty stdout after the change stops pf_verify"

for f in prodfail sysfail xargsfail; do
  build; run apply.sh >/dev/null; touch $B/$f; out="$(run rollback.sh)"; rm -f $B/$f
  echo "$out" | grep -q '^rollback INCOMPLETE'; t $? "rollback with $f reports INCOMPLETE"
  echo "$out" | grep -q '^rollback OK'; [ $? != 0 ]; t $? "rollback with $f: never OK"
  out="$(run rollback.sh)"; echo "$out" | grep -q '^rollback OK'; t $? "rollback OK once $f clears"
done
[ "$(devstate)" = "$DEV0" ]; t $? "DEV tree untouched across all tests"

pkill -u hyperchat-prod sleep; pkill -u hyperchat sleep; rm -rf $MOCKS /var/tmp/elsewhere $B
echo "passed=$pass failed=$failn"
