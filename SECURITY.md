# Security policy

Please **don't open public issues** for security problems. Report them
privately through GitHub's *Report a vulnerability* button (Security tab →
Advisories). If that isn't available, open an issue asking for a private
contact, without any details.

Two kinds of report are in scope:

1. **Privacy leaks.** Ways captured data (screenshots, window titles,
   messaging content) can leak to anyone other than the parent: other local
   users, the network, logs, backups or swap.
2. **Silent bypasses.** Ways the monitored (non-admin) user can disable or
   evade capture or enforcement *without it showing up* in the heartbeat,
   health check or dashboard. A bypass the parent can see is still worth
   reporting, but it is lower priority.

A monitored user with root or admin rights, or with physical access to
firmware and boot settings, is out of scope. familymon assumes the child uses
a standard account on a machine whose boot is locked down.

Known, deliberate gaps are listed in the README. Please check there first.
