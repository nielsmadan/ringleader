use std::fs::{OpenOptions, TryLockError};
use std::io::{self, Write};

fn main() {
    let mut args = std::env::args_os().skip(1);
    let path = args.next().expect("lock path");
    let hold = args.next().is_some();
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .expect("open native lock");

    match file.try_lock() {
        Ok(()) => {
            println!("locked");
            io::stdout().flush().expect("signal readiness");
            if hold {
                io::stdin()
                    .read_line(&mut String::new())
                    .expect("wait for release");
            }
        }
        Err(TryLockError::WouldBlock) => std::process::exit(35),
        Err(TryLockError::Error(err)) => panic!("{err}"),
    }
}
