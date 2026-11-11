# Storage and recovery

The installation has three independent locations:

| Location | Contents | Public Git repository? |
| --- | --- | --- |
| Code checkout / installed runtime | Reusable application code | Yes, code only |
| Private configuration directory | Source list, settings, small service logs, volume identity | No |
| Library data root | Database, captured objects, downloads, reader data, run records, backups | No |

For external storage, put the whole data root on the disk. Keep configuration and executables on the computer so a missing disk can be recognized. `pcl init` creates a `.pcl-storage.json` marker in a new root and, on macOS, records the actual volume UUID in private settings. Renaming the disk or moving the root requires updating the private configuration deliberately.

The supported `pcl` commands and configured jobs check the mount, identity marker and data-path containment. Missing or substituted storage does not initialize a replacement library or complete a weekly checkpoint. Mac launch agents check again on mount and login. Low-level collector commands that take `--state` directly do not know an installation's storage identity; use the configured entry points for normal operation.

Keep the disk connected during reading, updates and backups. Pause updates and stop the reader before ejecting it:

```sh
pcl schedule remove --config /absolute/path/to/settings.json
pcl reader remove --config /absolute/path/to/settings.json
```

Wait for any manual update to finish and eject through Finder. The guard detects absence at startup and workflow boundaries, and the reader supervisor checks while serving. It cannot make physically unplugging a disk during a write safe.

## Backups

Each weekly workflow exports neutral records, makes a SQLite snapshot with its referenced objects and audio queue, and verifies file hashes plus database integrity. With ArchiveBox configured, a separate native backup also preserves its database, rendered editions and annotations.

```sh
pcl backup --config /absolute/path/to/settings.json --output /absolute/path/to/new-backup
pcl verify-backup --path /absolute/path/to/new-backup
```

Restore into a **new empty directory**, preserving the database and relative object paths as one set. Verify the backup before switching configuration. Never merge databases or copy only the newest media files. Keep an unchanged original until search, annotations, original files and the reader have been checked in the restored installation.

Backups on the same external disk protect against some application mistakes; they do not protect against loss or failure of that disk. A second physical copy is a separate storage decision. Backup retention currently requires deliberate maintenance; the application does not automatically prune old runs.
