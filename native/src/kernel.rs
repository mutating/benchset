use std::hint::black_box;

const WORDS: usize = 32768;
const INPUT_BYTES: usize = 4096;

/// One worker owns a 256 KiB table and a 4 KiB structured input stream.
pub(crate) struct Kernel {
    table: Vec<u64>,
    input: [u8; INPUT_BYTES],
    state: u64,
}

impl Kernel {
    pub(crate) fn new(worker: usize) -> Result<Self, String> {
        Self::new_with_allocator(worker, &mut || allocate_table(WORDS))
    }

    fn new_with_allocator(
        worker: usize,
        allocate: &mut dyn FnMut() -> Result<Vec<u64>, String>,
    ) -> Result<Self, String> {
        let mut state = black_box(0x9e3779b97f4a7c15_u64 ^ worker as u64);
        let mut table = allocate()?;
        for _ in 0..WORDS {
            state = mix(state);
            table.push(state);
        }
        let mut input = [0; INPUT_BYTES];
        const ALPHABET: &[u8] = b"01234567890123456789 +-.,eE;xyz\n";
        for byte in &mut input {
            state = mix(state);
            *byte = ALPHABET[state as usize % ALPHABET.len()];
        }
        Ok(Self {
            table,
            input,
            state,
        })
    }

    #[inline(never)]
    pub(crate) fn step(&mut self) -> u64 {
        let mut value = self.state;
        // 256 dependent scalar integer rounds.
        for round in 0..256_u64 {
            value = value.wrapping_add(round ^ 0xa0761d6478bd642f);
            value ^= value.rotate_left(17);
            value = value.wrapping_mul(0xe7037ed1a0b428db);
            value ^= value >> 29;
        }

        // A stateful parser, with data-dependent transitions and an accumulator.
        let mut parser = (value & 3) as u8;
        for &byte in &self.input {
            match (parser, byte) {
                (_, b'0'..=b'9') => {
                    value = value.wrapping_mul(10).wrapping_add(u64::from(byte - b'0'));
                    parser = if parser == 2 { 3 } else { 1 };
                }
                (0, b'+' | b'-') => {
                    parser = 1;
                    value ^= u64::from(byte);
                }
                (1, b'.' | b',') => {
                    parser = 2;
                    value = value.rotate_left(7);
                }
                (1 | 3, b'e' | b'E') => {
                    parser = 0;
                    value = value.wrapping_add(97);
                }
                (_, b' ' | b';' | b'\n') => {
                    parser = 0;
                    value ^= value >> 11;
                }
                _ => {
                    parser = 0;
                    value = value.rotate_right(3) ^ u64::from(byte);
                }
            }
        }

        // 1,024 dependent indexed read/modify/write operations.
        let mut index = value as usize & (WORDS - 1);
        for round in 0..1024_u64 {
            let word = self.table[index];
            value = value.wrapping_add(word).rotate_left(13) ^ round;
            self.table[index] = word.wrapping_add(value ^ 0x8ebc6af09c88c6e3);
            index = (word ^ value) as usize & (WORDS - 1);
        }
        self.state = black_box(value ^ u64::from(parser));
        self.state
    }
}

fn allocate_table(words: usize) -> Result<Vec<u64>, String> {
    let mut table = Vec::new();
    table
        .try_reserve_exact(words)
        .map_err(|error| error.to_string())?;
    Ok(table)
}

fn mix(mut value: u64) -> u64 {
    value ^= value >> 12;
    value ^= value << 25;
    value ^= value >> 27;
    value.wrapping_mul(0x2545f4914f6cdd1d)
}

#[cfg(test)]
#[path = "kernel_tests.rs"]
mod tests;
