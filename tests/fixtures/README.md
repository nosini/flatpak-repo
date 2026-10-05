`upstream-public-key.gpg` is the public signing key embedded in
https://nosini.github.io/gallery-dl-flatpak/gallery-dl.flatpakrepo, retrieved
on 2026-10-05. It lets the collector rejection test import a real public key
and confirm that an unsigned source summary fails verification. No private
key is used by the tests.
