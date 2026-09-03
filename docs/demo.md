# Demo instance

`scripts/seed-demo.py` stands up a throwaway Pullbackup on your own machine and
fills it with a small **invented** homelab. It exists for one reason: the
screenshots in `README.md` and anywhere else Pullbackup is shown publicly.

A maintainer's real instance is not usable for that. It contains real hostnames,
real dataset paths, real usernames and the shape of a private network. So the
pictures come from a disposable instance built from the current checkout,
holding data that has never existed.

## Run it

```sh
python3 scripts/seed-demo.py
```

Docker is the only requirement; the script is standard library only. It builds
the image from the repository root, starts a container, waits for the app to
come up, seeds the data through the API, writes the run history and logs, and
then verifies the result. It prints the URL and credentials at the end:

```
Pullbackup demo:  http://127.0.0.1:18080/
username:         demo
password:         demo-instance-not-a-real-password-0000
```

Those credentials are deliberately public and deliberately silly. Do not reuse
them. The password is long only because the app refuses to start with an HTTP
Basic password under 32 characters.

Other modes:

```sh
python3 scripts/seed-demo.py --no-build   # reuse the local image; much faster
python3 scripts/seed-demo.py --verify     # re-run the safety checks only
python3 scripts/seed-demo.py --down       # remove the container and scratch dir
```

Re-running the plain form is safe. It tears the previous instance down first and
draws every "random" value from a fixed seed, so two runs produce the same data.
The container is `pullbackup-demo`, published as `127.0.0.1:18080:8000`; its
volumes live under `/tmp/pullbackup-demo`, which the script owns and deletes.

## It is reachable from your machine only

The credentials above are printed on stdout and written into this file, so the
instance must not be reachable from anywhere else. The publish spec names the
loopback address explicitly — `-p 127.0.0.1:18080:8000`. Docker's shorthand
`-p 18080:8000` would bind `0.0.0.0` **and** the IPv6 wildcard, putting those
known credentials on every interface the host has, including the LAN.

`--verify` re-proves the binding against the live container with `docker port`
and `docker inspect` rather than trusting this paragraph, and
`tests/test_demo_publish_binding.py` fails in CI if the publish spec ever loses
its host IP. You can check by hand too:

```sh
docker port pullbackup-demo 8000/tcp   # => 127.0.0.1:18080, and nothing else
curl -sS -m 3 http://$(ipconfig getifaddr en0):18080/api/system/health  # must fail
```

## What gets seeded

Two sources (`fileserver.example.internal`, `nas.example.internal`) and eleven
tasks: eight rsync, three syncoid. The mix is chosen so a screenshot has
something to show in every column — hourly through monthly schedules, two
disabled tasks so the disabled styling appears, sizes from 400 MB to 900 GB, and
about forty runs spread over the last fortnight including one currently running,
one queued, and three failures with real-looking rsync and syncoid errors.

Sources and tasks are created through the HTTP API rather than written to the
database, because `POST /api/tasks` is what registers the APScheduler job — and
without a registered job the "next run" column is empty for every row. Runs are
written directly to SQLite, because there is no API that creates a run without
executing a backup.

Byte counts, file counts and durations are derived from one another rather than
drawn independently, so they agree: a maildir of a million small messages moves
at a fraction of the rate a FLAC library does, and no run puts 900 GB across in
three seconds.

## It cannot reach a real host

The seeded tasks carry realistic cron schedules, which means APScheduler will
genuinely try to fire them. Four independent things stop that from becoming an
outbound connection, and `--verify` checks all of them against the running
container rather than asserting them in a comment:

1. **DNS is blackholed.** The container runs with `--dns 127.0.0.1`, so nothing
   resolves — not the invented hosts, not anything else.
2. **The hosts do not exist.** `.internal` is reserved by ICANN for private use
   and never resolves publicly; these particular names resolve nowhere at all.
3. **Admission is blocked.** Each source owns one non-terminal run row. The
   runner refuses to admit a new run while any run on the same *source* is
   pending or running, so every scheduled fire is denied before a command is
   built. This is the layer that actually stops the attempt.
4. **There is no key.** The seeded `ssh_key_path` points at a file the container
   does not have. (Deliberately not the key the app generates for itself at
   startup.)

`--verify` also confirms the Basic-auth behaviour worth screenshotting: a
browser-shaped request gets a `401` with **no** `WWW-Authenticate` header, so the
browser shows the app's own sign-in page instead of its native credential
dialog.

## Taking screenshots

Sign in, then capture at 1280px for the desktop table and 390px for the phone
card layout. Worth including: the task list (the type filter chips, the
run-state pills, the disabled rows), a run-detail page for both a successful and
a failed run, the run history for an hourly task, and the sign-in page.
