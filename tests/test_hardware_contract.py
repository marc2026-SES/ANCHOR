import shutil
import subprocess
from pathlib import Path

import pytest


def test_firmware_profile_clock_and_expiration(tmp_path):
    compiler = shutil.which('c++')
    if not compiler:
        pytest.skip('Host C++ compiler unavailable')
    source = tmp_path / 'profile.cpp'
    include = Path(__file__).resolve().parents[1] / 'hardware/firmware/libraries/AnchorNode'
    source.write_text('''#include <cassert>
#include "ReportProfile.h"
int main() {
  AnchorReportProfile p;
  assert(p.current(100) == 1000);
  assert(!p.valid(199, 1000)); assert(!p.valid(201, 1000));
  assert(!p.valid(200, 300001)); assert(p.valid(200, 30000));
  p.intervalMs = 200; p.untilMs = 5000;
  assert(p.current(0) == 1000); assert(p.current(4999) == 200);
  assert(p.current(5000) == 1000); assert(p.current(9000) == 1000);
}
''')
    binary = tmp_path / 'profile'
    subprocess.run([compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-I', str(include), str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
