use std::fs::File;
use std::io::Result;
use std::os::unix::fs::FileExt;

pub enum Seg {
    Src { offset: u64, len: u64 },
    Blob(Vec<u8>),
}

fn seg_len(s: &Seg) -> u64 {
    match s {
        Seg::Src { len, .. } => *len,
        Seg::Blob(b) => b.len() as u64,
    }
}

/// Up to `size` bytes at virtual `offset`; shorter only at end of file.
pub fn read_at(segs: &[Seg], src: &File, offset: u64, size: u32) -> Result<Vec<u8>> {
    let end = offset.saturating_add(size as u64);
    let mut out = Vec::new();
    let mut start = 0u64; // virtual start of the current segment
    for s in segs {
        let len = seg_len(s);
        let (lo, hi) = (offset.max(start), end.min(start + len));
        if lo < hi {
            let (from, n) = ((lo - start) as usize, (hi - lo) as usize);
            match s {
                Seg::Blob(b) => out.extend_from_slice(&b[from..from + n]),
                Seg::Src { offset: so, .. } => {
                    let at = out.len();
                    out.resize(at + n, 0);
                    src.read_exact_at(&mut out[at..], so + from as u64)?;
                }
            }
        }
        start += len;
        if start >= end {
            break;
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str, data: &[u8]) -> (std::path::PathBuf, File) {
        let path =
            std::env::temp_dir().join(format!("beetsfs-splice-{name}-{}", std::process::id()));
        std::fs::write(&path, data).unwrap();
        let f = File::open(&path).unwrap();
        (path, f)
    }

    #[test]
    fn every_range_matches_the_flat_file() {
        let (path, src) = tmp("flat", b"0123456789abcdef");
        let segs = vec![
            Seg::Blob(b"AAAA".to_vec()),
            Seg::Src { offset: 2, len: 3 },
            Seg::Blob(b"BB".to_vec()),
            Seg::Src { offset: 10, len: 4 },
        ];
        let flat = b"AAAA234BBabcd";
        for off in 0..=15u64 {
            for size in 0..=15u32 {
                let want = &flat[(off as usize).min(13)..((off + size as u64) as usize).min(13)];
                assert_eq!(
                    read_at(&segs, &src, off, size).unwrap(),
                    want,
                    "off={off} size={size}"
                );
            }
        }
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn truncated_source_is_an_error() {
        let (path, src) = tmp("short", b"0123456789abcdef");
        // Src segment claims bytes the file does not have
        assert!(read_at(
            &[Seg::Src {
                offset: 10,
                len: 100
            }],
            &src,
            0,
            50
        )
        .is_err());
        std::fs::remove_file(path).unwrap();
    }
}
