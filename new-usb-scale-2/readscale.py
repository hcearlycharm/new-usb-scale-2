#!/usr/bin/env python
# This program can be distributed under the terms of the GNU GPL.
# See the file COPYING.

import sys

if sys.platform == 'win32':
    import usb.core
    import usb.util
    import usb
else:
    import hid


class USBScaleBase(object):
    VENDOR_ID = 0x0922
    PRODUCT_ID = 0x8009
    DATA_MODE_GRAMS = 2
    DATA_MODE_OUNCES = 11

    def __init__(self):
        self.data = [0, 0, 0, 0, 0, 0]
        self.raw_weight = None

    def update(self):
        self.read()

    def read(self):
        raise NotImplementedError

    @property
    def corrected_raw_weight(self):
        """
        The corrected weight in ounces.
        data[3] is a signed scaling exponent (e.g. 255 = -1, meaning multiply by 10^-1 = 0.1)
        """
        weight = 0
        # Convert signed byte to actual exponent
        scaling = self.data[3] if self.data[3] < 128 else self.data[3] - 256
        multiplier = 10 ** scaling

        if self.data[2] == self.DATA_MODE_OUNCES:
            weight = self.raw_weight * multiplier
        elif self.data[2] == self.DATA_MODE_GRAMS:
            grams = self.raw_weight * multiplier
            weight = grams * 0.035274
        return weight

    @property
    def pounds(self):
        return self.corrected_raw_weight // 16

    @property
    def ounces(self):
        return self.corrected_raw_weight % 16


class USBScaleWin(USBScaleBase):
    def __init__(self):
        super(USBScaleWin, self).__init__()

        # Find the USB device
        self.device = usb.core.find(idVendor=self.VENDOR_ID,
                                    idProduct=self.PRODUCT_ID)

        # If the device isn't found, bail
        if not self.device:
            raise ValueError('Cannot find device')

        # Reset the device first to release any existing interface locks
        try:
            self.device.reset()
            print("Device reset successfully.", flush=True)
        except Exception as e:
            print(f"Reset warning (safe to ignore): {e}", flush=True)

        # Use the first/default configuration
        try:
            self.device.set_configuration()
        except usb.core.USBError as e:
            print(f"set_configuration warning (safe to ignore): {e}", flush=True)

        # Release and reclaim the interface to clear any locks
        try:
            usb.util.release_interface(self.device, 0)
        except Exception:
            pass
        try:
            usb.util.claim_interface(self.device, 0)
            print("Interface claimed successfully.", flush=True)
        except Exception as e:
            print(f"claim_interface warning: {e}", flush=True)

        # Get the first endpoint
        self.endpoint = self.device[0][(0, 0)][0]
        self.raw_weight = self.read()

    def read(self):
        """
        Read one data packet from the scale.
        Tries up to 20 times with a 500ms timeout each attempt.
        Returns last known weight silently if no new data arrives.
        """
        for attempt in range(20):
            try:
                data = self.device.read(
                    self.endpoint.bEndpointAddress,
                    self.endpoint.wMaxPacketSize,
                    timeout=500
                )
                # Skip empty or incomplete reads
                if not data or len(data) < 6:
                    continue

                # Got a valid reading
                raw = data[4] + data[5] * 256
                if raw > 32767:
                    raw = raw - 65536
                self.raw_weight = raw
                self.data = data
                return self.raw_weight

            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as e:
                strerror = str(e.strerror) if e.strerror is not None else ''
                if 'time' in strerror:
                    continue
                raise e

        # All attempts timed out — return last known weight silently
        return self.raw_weight

    def __del__(self):
        try:
            usb.util.dispose_resources(self.device)
        except Exception:
            pass


class USBScaleMac(USBScaleBase):
    def __init__(self):
        super(USBScaleMac, self).__init__()
        self.device = hid.device()
        try:
            self.device.open(self.VENDOR_ID, self.PRODUCT_ID)
        except IOError:
            sys.stdout.write("\rDevice appears to be busy, please check that "
                             "it is not in use by another process")
            sys.stdout.flush()
        self.device.set_nonblocking(1)
        self.raw_weight = self.read()

    def read(self):
        empty = False
        while True:
            data = self.device.read(64)
            if not data:
                empty = True
            if data and empty:
                break
        self.raw_weight = data[4] + data[5] * 256
        self.data = data
        return self.raw_weight

    def __del__(self):
        self.device.close()


def system_type():
    if sys.platform == 'darwin':
        return 'Mac'
    elif sys.platform == 'win32':
        return 'Win'
    else:
        raise NotImplementedError('The current system type is not supported')


def set_scale():
    scale = globals()['USBScale' + system_type()]()
    return scale


if __name__ == '__main__':
    print("Content-type: text/javascript\r\n\r\n")
    scale = set_scale()
    pounds, ounces = scale.pounds, scale.ounces
    print('here_is_the_weight({pounds:' + str(pounds) + ',ounces:' + str(round(ounces, 2)) + '})')