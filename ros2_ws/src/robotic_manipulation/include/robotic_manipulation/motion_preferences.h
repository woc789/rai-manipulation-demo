#pragma once

#include <cmath>
#include <algorithm>
#include <limits>
#include <vector>
#include <array>
#include <string>

namespace motion_preferences
{
inline double PregraspHeight(double contact_z)
{
  // Use a local descent, not a long vertical corridor next to the arm base.
  return std::max(contact_z + 0.08, 0.20);
}
inline bool ClearOfLimits(double value, double low, double high, double margin = 0.02)
{
  return std::isfinite(value) && std::isfinite(low) && std::isfinite(high) &&
    margin >= 0.0 && value > low + margin && value < high - margin;
}
inline std::array<double, 4> AlignAxialYaw(const std::array<double, 4> & target,
  const std::array<double, 4> & current, double offset = 0.0)
{
  auto yaw = [](const auto & q) {
      return std::atan2(2 * (q[0] * q[1] + q[3] * q[2]),
        1 - 2 * (q[1] * q[1] + q[2] * q[2]));
    };
  const double half = (yaw(current) - yaw(target) + offset) / 2;
  const double c = std::cos(half), s = std::sin(half);
  // Left multiplication by a world-Z rotation: never tilt the suction axis.
  return {c * target[0] - s * target[1], c * target[1] + s * target[0],
    c * target[2] + s * target[3], c * target[3] - s * target[2]};
}

inline std::vector<double> OrderedJoints(const std::vector<std::string> & names,
  const std::vector<double> & values)
{
  if (names.size() != values.size()) return {};
  std::vector<double> ordered;
  for (int joint = 1; joint <= 7; ++joint) {
    auto it = std::find(names.begin(), names.end(), "panda_joint" + std::to_string(joint));
    if (it == names.end()) return {};
    ordered.push_back(values[std::distance(names.begin(), it)]);
  }
  return ordered;
}

inline double MaxExcursion(const std::vector<std::vector<double>> & points)
{
  if (points.empty()) return std::numeric_limits<double>::infinity();
  auto low = points.front(), high = low;
  for (const auto & point : points) {
    if (point.size() != 7) return std::numeric_limits<double>::infinity();
    for (std::size_t i = 0; i < 7; ++i) {
      if (!std::isfinite(point[i])) return std::numeric_limits<double>::infinity();
      low[i] = std::min(low[i], point[i]); high[i] = std::max(high[i], point[i]);
    }
  }
  double range = 0.0;
  for (std::size_t i = 0; i < 7; ++i) range = std::max(range, high[i] - low[i]);
  return range;
}
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
