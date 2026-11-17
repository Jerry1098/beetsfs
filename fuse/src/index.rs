use crate::splice::Seg;
use rusqlite::{Connection, OpenFlags, OptionalExtension, Result, Row};
use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const ROOT_INO: u64 = 1;
pub const FILE_INO_BASE: u64 = 1 << 32; // dirs.id is assumed below this: kinds are told apart by inode

pub struct FileEntry {
    pub item_id: i64,
    pub src_path: PathBuf,
    pub src_size: u64,
    pub src_mtime_ns: i64,
    pub size: u64,
    pub mtime: i64,
}

pub enum Node {
    Dir { parent: u64 },
    File(FileEntry),
}

/// A corrupt or foreign index (rusqlite has no plain "bad data" error; this one prints m as is).
fn bad(m: String) -> rusqlite::Error {
    rusqlite::Error::ToSqlConversionFailure(m.into())
}

/// The index read-only; an error unless it has the layout this daemon reads (user_version 2).
pub fn open_ro(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    conn.busy_timeout(Duration::from_secs(5))?;
    let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if v != 2 {
        return Err(bad(format!("index user_version is {v}, not 2")));
    }
    Ok(conn)
}

const FILE: &str =
    "SELECT item_id, src_path, src_size, src_mtime_ns, size, mtime FROM files WHERE ";

// ids, inodes and sizes are non-negative by construction; checked anyway
fn uint(r: &Row, i: usize) -> Result<u64> {
    let v: i64 = r.get(i)?;
    u64::try_from(v).map_err(|_| rusqlite::Error::IntegralValueOutOfRange(i, v))
}

fn file(r: &Row) -> Result<Node> {
    Ok(Node::File(FileEntry {
        item_id: r.get(0)?,
        src_path: PathBuf::from(OsString::from_vec(r.get(1)?)),
        src_size: uint(r, 2)?,
        src_mtime_ns: r.get(3)?,
        size: uint(r, 4)?,
        mtime: r.get(5)?,
    }))
}

pub fn node(c: &Connection, ino: u64) -> Result<Option<Node>> {
    if ino >= FILE_INO_BASE {
        let id = (ino - FILE_INO_BASE) as i64;
        return c
            .prepare_cached(&format!("{FILE}item_id = ?"))?
            .query_row([id], file)
            .optional();
    }
    c.prepare_cached("SELECT parent FROM dirs WHERE id = ?")?
        .query_row([ino as i64], |r| {
            Ok(Node::Dir {
                parent: uint(r, 0)?,
            })
        })
        .optional()
}

/// A directory of that name wins over a file of the same name.
pub fn lookup(c: &Connection, parent: u64, name: &[u8]) -> Result<Option<(u64, Node)>> {
    let p = parent as i64;
    let dir = c
        .prepare_cached("SELECT id FROM dirs WHERE parent = ? AND name = ?")?
        .query_row((p, name), |r| uint(r, 0))
        .optional()?;
    if let Some(id) = dir {
        return Ok(Some((id, Node::Dir { parent })));
    }
    c.prepare_cached(&format!("{FILE}dir_id = ? AND name = ?"))?
        .query_row((p, name), |r| Ok((FILE_INO_BASE + uint(r, 0)?, file(r)?)))
        .optional()
}

/// Calls `add(inode, name)` for each entry of directory `ino` with an inode >= `from`: child
/// directories by id, then files by item_id (dir inodes < 2^32 <= file inodes, so the inode is
/// the order), skipping files hidden by a directory of the same name. Stops when `add` returns
/// true (reply full), so a page reads only the rows it returns.
pub fn children(
    c: &Connection,
    ino: u64,
    from: u64,
    mut add: impl FnMut(u64, &[u8]) -> bool,
) -> Result<()> {
    let from = from.min(i64::MAX as u64) as i64;
    for sql in ["SELECT id, name FROM dirs WHERE parent = ?1 AND id >= ?2 ORDER BY id",
                "SELECT item_id + 4294967296, name FROM files f WHERE dir_id = ?1 AND item_id >= ?2 - 4294967296 \
                   AND NOT EXISTS (SELECT 1 FROM dirs d WHERE d.parent = ?1 AND d.name = f.name) ORDER BY item_id"] {
        let mut st = c.prepare_cached(sql)?;
        let mut rows = st.query((ino as i64, from))?;
        while let Some(r) = rows.next()? {
            if add(uint(r, 0)?, &r.get::<_, Vec<u8>>(1)?) {
                return Ok(());
            }
        }
    }
    Ok(())
}

pub fn load_segments(conn: &Connection, item_id: i64) -> Result<Vec<Seg>> {
    let mut st = conn.prepare_cached(
        "SELECT s.blob_hash, s.offset, s.len, b.data FROM segments s \
         LEFT JOIN blobs b ON b.hash = s.blob_hash WHERE s.item_id = ? ORDER BY s.seq",
    )?;
    let bad = |m: &str| bad(format!("item {item_id}: {m}"));
    let rows = st.query_map([item_id], |r| {
        Ok((
            r.get::<_, Option<Vec<u8>>>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, i64>(2)?,
            r.get::<_, Option<Vec<u8>>>(3)?,
        ))
    })?;
    let mut out = Vec::new();
    for row in rows {
        let (hash, off, len, data) = row?;
        if off < 0 || len < 0 {
            return Err(bad("negative segment offset or length"));
        }
        let (off, len) = (off as u64, len as u64);
        out.push(match (hash, data) {
            (None, _) => Seg::Src { offset: off, len },
            (Some(_), None) => return Err(bad("missing blob")),
            (Some(_), Some(d)) => {
                let end = off.checked_add(len).filter(|&e| e <= d.len() as u64);
                Seg::Blob(
                    d[off as usize..end.ok_or_else(|| bad("range outside blob"))? as usize]
                        .to_vec(),
                )
            }
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    // the plugin's own schema, so the two cannot drift
    fn schema() -> &'static str {
        let py = include_str!("../../plugin/beetsplug/beetsfs.py");
        py.split("SCHEMA = \"\"\"")
            .nth(1)
            .unwrap()
            .split("\"\"\"")
            .next()
            .unwrap()
    }

    fn tmp_db(name: &str, sql: &str) -> PathBuf {
        let p =
            std::env::temp_dir().join(format!("beetsfs-index-{name}-{}.db", std::process::id()));
        let _ = std::fs::remove_file(&p);
        Connection::open(&p)
            .unwrap()
            .execute_batch(&format!("{}{sql}", schema()))
            .unwrap();
        p
    }

    fn e(i: u64, n: &[u8]) -> (u64, Vec<u8>) {
        (i, n.to_vec())
    }
    /// Entries from inode `from` on, `page` at a time, each page starting after the last one.
    fn list(c: &Connection, ino: u64, mut from: u64, page: usize) -> Vec<(u64, Vec<u8>)> {
        let mut out: Vec<(u64, Vec<u8>)> = vec![];
        loop {
            let n = out.len();
            children(c, ino, from, |i, name| {
                out.push(e(i, name));
                out.len() - n == page
            })
            .unwrap();
            match out.last() {
                Some(l) if out.len() > n => from = l.0 + 1,
                _ => return out,
            }
        }
    }

    #[test]
    fn queries() {
        // A(2)/B(3)/{1.mp3, 2.mp3}; A/B is also a file (hidden); caf\xe9(4)/{x\xff.flac, neg (size -1)}; ok.mp3
        let p = tmp_db("q", "
            INSERT INTO dirs VALUES (2,1,x'41'),(3,2,x'42'),(4,1,x'636166e9');
            INSERT INTO files VALUES (2,x'412f422f322e6d7033',3,x'322e6d7033',x'2f73',10,1,22,x'00',5),
              (1,x'412f422f312e6d7033',3,x'312e6d7033',x'2f73',10,1,11,x'00',5),(7,x'412f42',2,x'42',x'2f73',10,1,10,x'00',5),
              (5,x'6f6b2e6d7033',1,x'6f6b2e6d7033',x'2f73',10,1,10,x'00',5),(9,x'636166e92f78ff2e666c6163',4,x'78ff2e666c6163',x'2f73',10,1,10,x'00',5),
              (6,x'636166e92f6e6567',4,x'6e6567',x'2f73',10,1,-1,x'00',5);");
        let c = open_ro(&p).unwrap();
        let f = |i: u64| FILE_INO_BASE + i;
        assert!(matches!(
            lookup(&c, 1, b"A").unwrap(),
            Some((2, Node::Dir { parent: 1 }))
        ));
        assert!(matches!(
            lookup(&c, 2, b"B").unwrap(),
            Some((3, Node::Dir { .. }))
        )); // dir wins
        assert!(
            matches!(lookup(&c, 3, b"1.mp3").unwrap(), Some((i, Node::File(e))) if i == f(1) && e.size == 11 && e.mtime == 5)
        );
        assert!(matches!(lookup(&c, 4, b"x\xff.flac").unwrap(), Some((i, _)) if i == f(9)));
        assert!(lookup(&c, 1, b"nope").unwrap().is_none());
        assert!(lookup(&c, 4, b"neg").is_err()); // negative size
        assert!(matches!(
            node(&c, ROOT_INO).unwrap(),
            Some(Node::Dir { parent: 0 })
        ));
        assert!(matches!(
            node(&c, 3).unwrap(),
            Some(Node::Dir { parent: 2 })
        ));
        assert!(
            matches!(node(&c, f(2)).unwrap(), Some(Node::File(e)) if e.item_id == 2 && e.size == 22)
        );
        assert!(node(&c, 99).unwrap().is_none() && node(&c, f(99)).unwrap().is_none());
        assert_eq!(
            list(&c, 1, 0, 100),
            [e(2, b"A"), e(4, b"caf\xe9"), e(f(5), b"ok.mp3")]
        );
        assert_eq!(list(&c, 1, 3, 100), [e(4, b"caf\xe9"), e(f(5), b"ok.mp3")]);
        assert_eq!(list(&c, 1, f(5) + 1, 100), []);
        assert_eq!(list(&c, 1, u64::MAX, 100), []);
        assert_eq!(list(&c, 2, 0, 100), [e(3, b"B")]);
        assert_eq!(list(&c, 3, 0, 1), [e(f(1), b"1.mp3"), e(f(2), b"2.mp3")]);
        drop(c);
        std::fs::remove_file(p).unwrap();
    }

    #[test]
    fn big_directory_in_pages() {
        // 300 dirs and 300 files in the root, file 5 hidden by dir "d5"
        let mut sql = String::new();
        for k in 0..300 {
            sql += &format!(
                "INSERT INTO dirs VALUES ({}, 1, CAST('d{k}' AS BLOB));",
                k + 2
            );
            let name = if k == 5 { "d5".into() } else { format!("f{k}") };
            sql += &format!("INSERT INTO files VALUES ({k}, CAST('{name}' AS BLOB), 1, CAST('{name}' AS BLOB), x'2f73',1,1,1,x'00',1);");
        }
        let p = tmp_db("big", &sql);
        let c = open_ro(&p).unwrap();
        let all = list(&c, 1, 0, 7);
        let want: Vec<u64> = (2..302)
            .chain((0..300).filter(|&k| k != 5).map(|k| FILE_INO_BASE + k))
            .collect();
        assert_eq!(all.iter().map(|x| x.0).collect::<Vec<_>>(), want);
        drop(c);
        std::fs::remove_file(p).unwrap();
    }

    #[test]
    fn segments_round_trip() {
        let p = tmp_db("segs", "
            INSERT INTO blobs VALUES (x'01', CAST('HELLOWORLD' AS BLOB));
            INSERT INTO files VALUES (1,x'61',1,x'61',x'2f73',10,1,10,x'00',1),(2,x'62',1,x'62',x'2f73',10,1,10,x'00',1),
              (3,x'63',1,x'63',x'2f73',10,1,10,x'00',1),(4,x'64',1,x'64',x'2f73',10,1,10,x'00',1);
            INSERT INTO segments VALUES (1,0,x'01',5,5),(1,1,NULL,100,20),(2,0,x'02',0,1),(3,0,x'01',8,5),(4,0,NULL,-1,5);");
        let c = open_ro(&p).unwrap();
        let segs = load_segments(&c, 1).unwrap();
        assert!(
            matches!(&segs[..], [Seg::Blob(b), Seg::Src { offset: 100, len: 20 }] if b == b"WORLD")
        );
        assert!(load_segments(&c, 2).is_err()); // missing blob
        assert!(load_segments(&c, 3).is_err()); // range outside blob
        assert!(load_segments(&c, 4).is_err()); // negative offset
        drop(c);
        std::fs::remove_file(p).unwrap();
    }

    #[test]
    fn missing_or_old_index_is_an_error() {
        assert!(open_ro(Path::new("/nonexistent/index.db")).is_err());
        let p =
            std::env::temp_dir().join(format!("beetsfs-index-absent-{}.db", std::process::id()));
        assert!(open_ro(&p).is_err());
        assert!(!p.exists());
        let p = tmp_db("v1", "PRAGMA user_version=1;");
        assert!(open_ro(&p).is_err());
        std::fs::remove_file(p).unwrap();
    }
}
