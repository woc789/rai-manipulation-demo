#pragma once

#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

namespace motion_guard
{
inline double MaxError(const std::vector<double> & a, const std::vector<double> & b)
{
  if (a.empty() || a.size() != b.size()) return std::numeric_limits<double>::infinity();
  double error = 0.0;
  for (std::size_t i = 0; i < a.size(); ++i) {
    if (!std::isfinite(a[i]) || !std::isfinite(b[i])) {
      return std::numeric_limits<double>::infinity();
    }
    error = std::max(error, std::abs(a[i] - b[i]));
  }
  return error;
}

// Require changing state timestamps and a bounded position range for 200 ms.
// A repeated stale message, or slow accumulated drift, must not count as settled.
class StabilityWindow
{
public:
  bool Observe(const std::vector<double> & positions, double stamp, double wall_time)
  {
    if (!std::isfinite(stamp) || !std::isfinite(wall_time) || stamp <= last_stamp_) {
      return false;
    }
    last_stamp_ = stamp;
    if (MaxError(positions, positions) != 0.0) {
      anchor_.clear();
      return false;
    }
    if (MaxError(positions, anchor_) > 0.001) {
      anchor_ = positions;
      since_ = wall_time;
      samples_ = 1;
      return false;
    }
    return ++samples_ >= 3 && wall_time - since_ >= 0.2;
  }

private:
  std::vector<double> anchor_;
  double last_stamp_ = -std::numeric_limits<double>::infinity();
  double since_ = 0.0;
  unsigned int samples_ = 0;
};
}  // namespace motion_guard
