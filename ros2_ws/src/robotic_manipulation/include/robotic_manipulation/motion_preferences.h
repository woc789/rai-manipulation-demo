#pragma once

#include <cmath>
#include <limits>
#include <vector>

namespace motion_preferences
{
// Panda joints are bounded, not continuous: do NOT wrap a near-full wrist turn.
inline double Distance(const std::vector<double> & a, const std::vector<double> & b)
{
  if (a.size() != 7 || b.size() != 7) return std::numeric_limits<double>::infinity();
  double sum = 0.0;
  for (std::size_t i = 0; i < a.size(); ++i) {
    if (!std::isfinite(a[i]) || !std::isfinite(b[i])) {
      return std::numeric_limits<double>::infinity();
    }
    const double weight = i == 6 ? 2.0 : 1.0;
    sum += weight * (a[i] - b[i]) * (a[i] - b[i]);
  }
  return std::sqrt(sum);
}

inline double PathLength(const std::vector<std::vector<double>> & points)
{
  if (points.empty()) return std::numeric_limits<double>::infinity();
  double length = Distance(points.front(), points.front());
  for (std::size_t i = 1; i < points.size(); ++i) length += Distance(points[i - 1], points[i]);
  return length;
}
}  // namespace motion_preferences
