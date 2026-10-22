# Nosini Flatpaks

One Flatpak remote, `nosini`, with all of these apps:

- [gallery-dl](https://github.com/nosini/gallery-dl-flatpak) downloads image
  galleries and collections from the command line, with optional add-ons.
- [Pulsemeeter](https://github.com/nosini/pulsemeeter-flatpak) routes audio
  between virtual and hardware devices.
- [Reddit Migration](https://github.com/nosini/reddit-migration) moves saved
  Reddit items from one account to another.
- [Artemis](https://github.com/nosini/artemis-flatpak) controls the RGB
  lighting of all your devices.
- [Teams Office](https://github.com/nosini/teams-office-flatpak) is a Teams
  client with Word and Excel access for local AI agents.

Each app is also available from its own remote, described in its
repository. Both remotes carry the same builds, so pick whichever you prefer:
this one if you use more than one of the apps.

## Installing

Install [Flatpak](https://flatpak.org/setup/) if you don't already have it.
Add Flathub, where the apps' runtimes come from, and this remote:

```sh
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak remote-add --user --if-not-exists nosini https://nosini.github.io/flatpak-repo/nosini.flatpakrepo
```

Then install the apps you want:

```sh
flatpak install --user nosini eu.nosini.GalleryDl
flatpak install --user nosini eu.nosini.Pulsemeeter
flatpak install --user nosini eu.nosini.RedditMigration
flatpak install --user nosini eu.nosini.Artemis
flatpak install --user nosini eu.nosini.TeamsOffice
```

The [download page](https://nosini.github.io/flatpak-repo/) has a link for
each app, which software centers such as GNOME Software and Discover open
directly. The apps also appear in your software center once the remote is
added.

Updates arrive like those of any other Flatpak, from your software center
or with `flatpak update --user`. All apps are available for x86_64, and
for aarch64 where the app supports it.

### gallery-dl add-ons

gallery-dl's optional features, such as yt-dlp for videos, are separate
add-ons. Install them from the same remote, so they update with the app:

```sh
flatpak install --user nosini eu.nosini.GalleryDl.YtDlp
```

The [add-on list](https://github.com/nosini/gallery-dl-flatpak/blob/main/docs/usage.md)
describes each one.

### Moving an installed app to this remote

An app installed from its own remote keeps updating from there. To have
it update from `nosini` instead:

```sh
flatpak install --user --reinstall nosini eu.nosini.GalleryDl
```

Do the same for each of its add-ons. Your settings and data are kept.

### Going back to an earlier version

The remote keeps the five previous versions of every app. If an update
causes problems, list them and go back to one by its commit:

```sh
flatpak remote-info --user --log nosini eu.nosini.Pulsemeeter
flatpak update --user --commit=COMMIT eu.nosini.Pulsemeeter
```

`flatpak mask --user eu.nosini.Pulsemeeter` then keeps it from updating
until you run `flatpak mask --user --remove eu.nosini.Pulsemeeter`.

## How the apps get here

Every app is built, tested and signed once, by its own repository, and
published to that repository's remote. This repository collects those
published packages, checks their signatures and signs them again with the
`nosini` key, without building anything. A new version shows up here
shortly after it is published on the app's own remote.

[docs/development.md](docs/development.md) explains the setup and how to
add an app.

## License

The scripts and configuration in this repository are licensed under the
[GNU Affero General Public License, version 3 or later](LICENSE). Each app
and its dependencies keep their own licenses.
