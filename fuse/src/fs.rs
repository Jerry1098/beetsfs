use crate::index::{self, Node, FILE_INO_BASE, ROOT_INO};
use crate::splice::{self, Seg};
use fuser::{
    Errno, FileAttr, FileHandle, FileType, Filesystem, FopenFlags, Generation, INodeNo, LockOwner,
    OpenFlags, ReplyAttr, ReplyData, ReplyDirectory, ReplyEmpty, ReplyEntry, ReplyOpen, Request,
};
use rusqlite::Connection;
use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs::File;
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

// ponytail: 1s TTL; use fuser's notifier if getattr traffic shows up in profiles
const TTL: Duration = Duration::from_secs(1);

struct Handle {
    file: File,
    segs: Vec<Seg>,
}

struct Db {
    conn: Option<Connection>, // opened lazily
    last_err: Option<String>, // logged once
}

pub struct BeetsFs {
    index_path: PathBuf,
    // fuser 0.18 calls every method with &self, hence the Mutexes
    db: Mutex<Db>,
    handles: Mutex<HashMap<u64, Arc<Handle>>>, // by fh = the source's fd, unique while open
    uid: u32,
    gid: u32,
    started: SystemTime,
}

impl BeetsFs {
    pub fn new(index_path: PathBuf) -> Self {
        // SAFETY: plain getters without side effects
        let (uid, gid) = unsafe { (libc::geteuid(), libc::getegid()) };
        BeetsFs {
            index_path,
            db: Mutex::new(Db {
                conn: None,
                last_err: None,
            }),
            handles: Mutex::new(HashMap::new()),
            uid,
            gid,
            started: SystemTime::now(),
        }
    }

    /// Run a query on the index; None on any error (logged once per distinct error, then the
    /// connection is reopened on the next call). Callers serve an empty root then.
    fn db<T>(&self, f: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> Option<T> {
        let mut g = self.db.lock().unwrap();
        let Db { conn, last_err } = &mut *g;
        let r = match conn {
            Some(c) => f(c),
            None => index::open_ro(&self.index_path).and_then(|c| f(conn.insert(c))),
        };
        match r {
            Ok(v) => {
                if last_err.take().is_some() {
                    eprintln!(
                        "beetsfs: index {} readable again",
                        self.index_path.display()
                    );
                }
                Some(v)
            }
            Err(e) => {
                let msg = e.to_string();
                if last_err.as_ref() != Some(&msg) {
                    eprintln!("beetsfs: index {}: {msg}", self.index_path.display());
                    *last_err = Some(msg);
                }
                if matches!(e, rusqlite::Error::SqliteFailure(..)) {
                    *conn = None; // reopen on the next call; a bad row (data error) keeps it
                }
                None
            }
        }
    }

    fn attr(&self, ino: u64, node: &Node) -> FileAttr {
        let (kind, perm, nlink, size, mtime) = match node {
            Node::Dir { .. } => (FileType::Directory, 0o555, 2, 0, self.started),
            Node::File(e) => (
                FileType::RegularFile,
                0o444,
                1,
                e.size,
                UNIX_EPOCH + Duration::from_secs(e.mtime.max(0) as u64),
            ),
        };
        FileAttr {
            ino: INodeNo(ino),
            size,
            blocks: size.div_ceil(512),
            atime: mtime,
            mtime,
            ctime: mtime,
            crtime: mtime,
            kind,
            perm,
            nlink,
            uid: self.uid,
            gid: self.gid,
            rdev: 0,
            flags: 0,
            blksize: 4096,
        }
    }
}

impl Filesystem for BeetsFs {
    fn lookup(&self, _req: &Request, parent: INodeNo, name: &OsStr, reply: ReplyEntry) {
        match self
            .db(|c| index::lookup(c, parent.into(), name.as_bytes()))
            .flatten()
        {
            Some((i, n)) => reply.entry(&TTL, &self.attr(i, &n), Generation(0)),
            None => reply.error(Errno::ENOENT),
        }
    }

    fn getattr(&self, _req: &Request, ino: INodeNo, _fh: Option<FileHandle>, reply: ReplyAttr) {
        let ino = u64::from(ino);
        match self.db(|c| index::node(c, ino)).flatten() {
            Some(n) => reply.attr(&TTL, &self.attr(ino, &n)),
            None if ino == ROOT_INO => reply.attr(&TTL, &self.attr(ino, &Node::Dir { parent: 0 })),
            None => reply.error(Errno::ENOENT),
        }
    }

    fn readdir(
        &self,
        _req: &Request,
        ino: INodeNo,
        _fh: FileHandle,
        offset: u64,
        mut reply: ReplyDirectory,
    ) {
        let ino = u64::from(ino);
        let parent = match self.db(|c| index::node(c, ino)).flatten() {
            Some(Node::Dir { parent }) => parent,
            _ if ino == ROOT_INO => 0, // no index: empty root
            _ => return reply.error(Errno::ENOTDIR),
        };
        let up = if parent == 0 { ino } else { parent };
        let kind = |i: u64| {
            if i >= FILE_INO_BASE {
                FileType::RegularFile
            } else {
                FileType::Directory
            }
        };
        let mut add = |i: u64, off: u64, name: &[u8]| {
            reply.add(INodeNo(i), off, kind(i), OsStr::from_bytes(name))
        };
        // Offsets: "." is 1, ".." is 2, a child is its inode + 1 (child inodes are >= 2), so the
        // next page is the children with inode >= offset.
        // add() returns true when the reply is full
        if offset < 1 && add(ino, 1, b".") {
            return reply.ok();
        }
        if offset < 2 && add(up, 2, b"..") {
            return reply.ok();
        }
        self.db(|c| index::children(c, ino, offset, |i, name| add(i, i + 1, name)));
        reply.ok();
    }

    // Write opens never get here (read-only mount: the kernel answers EROFS), nor do
    // directories (opendir).
    fn open(&self, _req: &Request, ino: INodeNo, _flags: OpenFlags, reply: ReplyOpen) {
        let ino = u64::from(ino);
        // row and segments from one snapshot
        let found = self.db(|c| {
            let tx = c.unchecked_transaction()?;
            match index::node(&tx, ino)? {
                Some(Node::File(e)) => Ok(Some((index::load_segments(&tx, e.item_id)?, e))),
                _ => Ok(None),
            }
        });
        let (segs, e) = match found {
            Some(Some(x)) => x,
            Some(None) => return reply.error(Errno::ENOENT), // vanished since the lookup
            None => return reply.error(Errno::EIO),
        };
        let (file, md) = match open_src(&e.src_path) {
            Ok(x) => x,
            Err(err) => return reply.error(err.into()),
        };
        let mtime_ns = md
            .mtime()
            .saturating_mul(1_000_000_000)
            .saturating_add(md.mtime_nsec());
        let (segs, fopen) = if md.size() != e.src_size || mtime_ns != e.src_mtime_ns {
            eprintln!(
                "beetsfs: {:?} changed since indexing; serving the original file",
                e.src_path
            );
            (
                vec![Seg::Src {
                    offset: 0,
                    len: md.size(),
                }],
                FopenFlags::FOPEN_DIRECT_IO,
            )
        } else {
            (segs, FopenFlags::empty()) // size = sum of segments: written and read in one snapshot
        };
        let fh = file.as_raw_fd() as u64;
        let h = Arc::new(Handle { file, segs });
        self.handles.lock().unwrap().insert(fh, h);
        reply.opened(FileHandle(fh), fopen);
    }

    fn read(
        &self,
        _req: &Request,
        _ino: INodeNo,
        fh: FileHandle,
        offset: u64,
        size: u32,
        _flags: OpenFlags,
        _lock_owner: Option<LockOwner>,
        reply: ReplyData,
    ) {
        // cloned out so the network read below does not hold the lock
        let h = self.handles.lock().unwrap().get(&fh.into()).cloned();
        let Some(h) = h else {
            return reply.error(Errno::EBADF);
        };
        match splice::read_at(&h.segs, &h.file, offset, size) {
            Ok(data) => reply.data(&data),
            Err(err) => {
                eprintln!("beetsfs: read fh {}: {err}", u64::from(fh));
                reply.error(Errno::EIO)
            }
        }
    }

    fn release(
        &self,
        _req: &Request,
        _ino: INodeNo,
        fh: FileHandle,
        _flags: OpenFlags,
        _lock_owner: Option<LockOwner>,
        _flush: bool,
        reply: ReplyEmpty,
    ) {
        self.handles.lock().unwrap().remove(&fh.into());
        reply.ok();
    }
}

/// Open a source file without blocking (a FIFO's read side would hang the daemon) and only if
/// it is a regular file. Symlinks are followed: libraries may contain symlinked files.
fn open_src(path: &Path) -> std::io::Result<(File, std::fs::Metadata)> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)?;
    let md = file.metadata()?;
    if !md.is_file() {
        return Err(std::io::Error::from_raw_os_error(libc::EIO));
    }
    Ok((file, md))
}

#[cfg(test)]
mod tests {
    #[test]
    fn fifo_source_is_an_error_not_a_hang() {
        let path = std::env::temp_dir().join(format!("beetsfs-fifo-{}", std::process::id()));
        let c = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        // SAFETY: valid C string
        assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
        let r = super::open_src(&path);
        std::fs::remove_file(&path).unwrap();
        assert_eq!(r.err().and_then(|e| e.raw_os_error()), Some(libc::EIO));
    }
}
