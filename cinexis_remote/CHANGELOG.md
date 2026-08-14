# Changelog

## 1.19.11

Clearer message when setup hasn't finished.

The panel used to say your account was "waiting for admin approval" and would be activated
"within 24 hours". Activation is automatic now, so that was misleading — if you saw that
screen, nobody was coming to approve anything and there was no wait that would fix it. It now
explains what's actually missing, what to do about it, and confirms your Home Assistant keeps
working normally in the meantime.

## 1.19.10

**Security — the WhatsApp pairing QR is now protected.**

The `/qr` endpoint on the WhatsApp service (published to your Home Assistant host
on port 18083) required no authentication. Anyone on the same network could fetch
the pairing QR, scan it, and link your WhatsApp account to their own device. It now
requires the same shared secret as the send endpoints. The Cinexis panel is
unaffected — it already authenticates.

Also in this release: clearer guidance if the add-on can't start because port 18083
is already in use (change it under Settings → Add-ons → Cinexis → Network).

## 1.19.9

**Security — add-on private storage.**

Cinexis previously stored its node identity, licence key and WhatsApp session in
`/share/cinexis`. Home Assistant maps `/share` into *every* add-on that requests
share access, so another add-on installed on the same machine could read those
files. Storage has moved to `/data/cinexis`, which is private to this add-on.

Existing installs migrate automatically on first start: your files are **copied**
(not moved) to the new location, so your node identity, remote-access address and
paired WhatsApp session all stay exactly the same. Nothing to do on your side, and
the old files are left in place so the update can be rolled back safely.
