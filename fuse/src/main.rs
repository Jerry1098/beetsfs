mod fs;
mod index;
mod splice;

use fuser::{Config, MountOption, Session, SessionACL};
use std::path::{Path, PathBuf};

fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() != 3 {
        eprintln!("usage: beetsfs <index.db> <mountpoint>");
        std::process::exit(2);
    }
    let index_path = PathBuf::from(&args[1]);

    // Block SIGTERM/SIGINT before any thread exists so every thread inherits the mask
    // and only the sigwait thread below receives them.
    // SAFETY: sigset manipulation on a local, initialised by sigemptyset.
    let sigs = unsafe {
        let mut s: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut s);
        libc::sigaddset(&mut s, libc::SIGTERM);
        libc::sigaddset(&mut s, libc::SIGINT);
        libc::pthread_sigmask(libc::SIG_BLOCK, &s, std::ptr::null_mut());
        s
    };

    let mut cfg = Config::default();
    cfg.n_threads = Some(4); // a slow source read must not stall lookups and other reads
    cfg.mount_options = vec![
        MountOption::RO,
        MountOption::FSName("beetsfs".into()),
        MountOption::DefaultPermissions,
    ];
    // SAFETY: plain getter
    let is_root = unsafe { libc::geteuid() } == 0;
    if is_root {
        // allow_other, so other users (and other containers) can read the view. A normal user
        // may not set it unless /etc/fuse.conf has user_allow_other.
        // No auto_unmount: with it fuser hands unmounting to the fusermount3 helper, which in a
        // rootless podman container exits without unmounting, so SIGTERM left the mount in place.
        // As root the container's PID 1 unmounts once the daemon has exited, also after a
        // SIGKILL (docs/DESIGN.md, Container).
        cfg.acl = SessionACL::All;
    }
    let mut session = match Session::new(fs::BeetsFs::new(index_path), &args[2], &cfg) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("beetsfs: mount {}: {e}", Path::new(&args[2]).display());
            std::process::exit(1);
        }
    };
    // Mounted: drop every capability. The daemon parses the index, which beet (the tag parser)
    // writes; nothing it does from here on needs one. As root (the container) it can then no
    // longer unmount: PID 1 does, lazily, once the daemon has exited.
    let header = [0x2008_0522u32, 0]; // _LINUX_CAPABILITY_VERSION_3, this process
    let empty = [0u32; 6]; // effective, permitted, inheritable; two 32-bit halves each
                           // SAFETY: valid pointers to the kernel's capset structs
    if unsafe { libc::syscall(libc::SYS_capset, header.as_ptr(), empty.as_ptr()) } != 0 {
        eprintln!("beetsfs: capset: {}", std::io::Error::last_os_error());
        std::process::exit(1);
    }
    // On SIGTERM/SIGINT: as a normal user, fuser's unmounter runs `fusermount3 -u -z` (lazy: a
    // plain umount fails with EBUSY while a consumer holds a file or its cwd in the view, and
    // session.run() only returns once the last reference is gone); then exit.
    let mut unmounter = session.unmount_callable();
    std::thread::spawn(move || {
        let mut sig = 0;
        // SAFETY: sigs is a valid set, sig a valid out pointer
        unsafe { libc::sigwait(&sigs, &mut sig) };
        if !is_root {
            if let Err(e) = unmounter.unmount() {
                eprintln!("beetsfs: unmount after signal {sig}: {e}");
                std::process::exit(1);
            }
        }
        std::process::exit(0);
    });
    if let Err(e) = session.run() {
        eprintln!("beetsfs: {e}");
        std::process::exit(1);
    }
}
