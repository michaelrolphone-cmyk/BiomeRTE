"""Patch the pinned Adafruit BNO08x SPI HAL and bound optional-sensor waits."""

import hashlib
from pathlib import Path


ORIGINAL = {
    "Adafruit_BNO08x.cpp": "0cd1fc5e591a5cb6e7b91cde71b935acd229fcbc",
    "sh2.c": "d2f00f0b00d2d80efa78f10ac58e2500aa55fb4c",
    "shtp.c": "503b5eb5d3b001862987b90cbfad68648054543e",
}


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"BNO08x patch context changed: {old[:72]}")
    return text.replace(old, new)


def patch_spi(text):
    start = text.index('  // Serial.println("SPI HAL open");')
    end = text.index("\n/**************************************** HAL interface", start)
    return text[:start] + """  return 0; // Reset and initial INT detection are owned by the sensor worker.
}

static void spihal_close(sh2_Hal_t *self) {}

static int spihal_read(sh2_Hal_t *self, uint8_t *buffer, unsigned len,
                      uint32_t *timestamp) {
  *timestamp = micros();
  if (len < 4 || digitalRead(_int_pin)) {
    delay(1);
    return 0;
  }

  // INT may go high once CS is asserted. Read the header and body under
  // one assertion, as in CEVA's SH2 SPI reference transport.
  memset(buffer, 0, 4);
  spi_dev->beginTransactionWithAssertingCS();
  spi_dev->transfer(buffer, 4);
  const unsigned size = (buffer[0] | (unsigned(buffer[1]) << 8)) & 0x7fff;
  if (size > 4 && size <= len) {
    memset(buffer + 4, 0, size - 4);
    spi_dev->transfer(buffer + 4, size - 4);
  }
  spi_dev->endTransactionWithDeassertingCS();
  return size > 4 && size <= len ? int(size) : 0;
}

static int spihal_write(sh2_Hal_t *self, uint8_t *buffer, unsigned len) {
  if (digitalRead(_int_pin)) {
    delay(1);
    return 0; // Bounded retry in SHTP. Do not reset the sensor from the HAL.
  }
  return spi_dev->write(buffer, len) ? int(len) : -1;
}
""" + text[end:]


def patch_sh2(text):
    text = replace(text, "((pOp->timeout_us == 0) ||\n"
                   "            ((now_us-start_us) < pOp->timeout_us))",
                   "((now_us-start_us) < (pOp->timeout_us ? pOp->timeout_us : 2000000U))")
    return replace(text, "    shtp_close(pSh2->pShtp);",
                   "    if (pSh2->pShtp != 0) shtp_close(pSh2->pShtp);")


def patch_shtp(text):
    text = replace(text, "        // Transmit (try repeatedly while HAL write returns 0)",
                   "        uint32_t writeStartUs = pShtp->pHal->getTimeUs(pShtp->pHal);")
    text = replace(text, "while (status == 0)",
                   "while (status == 0 &&\n"
                   "               (uint32_t)(pShtp->pHal->getTimeUs(pShtp->pHal) - writeStartUs) < 2000000U)")
    return replace(text, "        if (status < 0)",
                   "        if (status == 0) status = SH2_ERR_TIMEOUT;\n"
                   "        if (status < 0)")


def apply(libdeps):
    (source,) = tuple(libdeps.glob("*/src/Adafruit_BNO08x.cpp"))
    source = source.parent
    edits = {}
    for name, transform in zip(ORIGINAL, (patch_spi, patch_sh2, patch_shtp)):
        path = source / name
        data = path.read_bytes()
        marker = b"// BiomeRTE BNO08x SPI startup patch\n"
        if data.startswith(marker):
            continue
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if blob != ORIGINAL[name]:
            raise ValueError(f"Unexpected Adafruit BNO08x 1.2.5 source: {name}")
        edits[path] = marker + transform(data.decode()).encode()
    for path, data in edits.items():
        path.write_bytes(data)


if "Import" in globals():
    Import("env")
    apply(Path(env.subst("$PROJECT_LIBDEPS_DIR")) / env.subst("$PIOENV"))
