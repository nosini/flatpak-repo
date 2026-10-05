# Nosini Flatpaks

A shared Flatpak remote for [gallery-dl](https://github.com/nosini/gallery-dl-flatpak),
[Pulsemeeter](https://github.com/nosini/pulsemeeter-flatpak), and
[Reddit Migration](https://github.com/nosini/reddit-migration).
It includes gallery-dl's optional add-ons. Packages are available for x86_64.

After the first publication, add the remote and install the apps you want:

```sh
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak remote-add --user --if-not-exists nosini https://nosini.github.io/flatpak-repo/nosini.flatpakrepo
flatpak install --user nosini eu.nosini.GalleryDl
flatpak install --user nosini eu.nosini.Pulsemeeter
flatpak install --user nosini eu.nosini.RedditMigration
```

You can also install an app by opening its link on the
[download page](https://nosini.github.io/flatpak-repo/).
Application runtimes come from Flathub. Run `flatpak update --user` to update
your installed apps, or use your desktop's software manager.

If you already installed an app from its separate remote or a local build,
switch it to this remote with:

```sh
flatpak install --user --reinstall nosini eu.nosini.GalleryDl//stable
flatpak install --user --reinstall nosini eu.nosini.Pulsemeeter//stable
flatpak install --user --reinstall nosini eu.nosini.RedditMigration//master
```

For optional gallery-dl features, see its
[add-on instructions](https://github.com/nosini/gallery-dl-flatpak/blob/main/docs/usage.md).
Install add-ons from `nosini` so they update with the app; for example:

```sh
flatpak install --user nosini eu.nosini.GalleryDl.YtDlp//stable
```

[Repository setup and building](docs/development.md) explains signing,
publishing, local builds, and adding more apps.

The scripts and configuration in this repository are licensed under
[GNU AGPL v3 or later](LICENSE). Each packaged application and its dependencies
retain their own licenses.
