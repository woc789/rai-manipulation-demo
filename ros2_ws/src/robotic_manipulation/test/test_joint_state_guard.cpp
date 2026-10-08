#include <gtest/gtest.h>
#include "robotic_manipulation/joint_state_guard.h"
#include "robotic_manipulation/motion_preferences.h"

TEST(MotionPreferences, PregraspUsesLocalDescentWithTableClearance)
{
  EXPECT_NEAR(motion_preferences::PregraspHeight(0.14), 0.22, 1e-12);
  EXPECT_NEAR(motion_preferences::PregraspHeight(0.10), 0.20, 1e-12);
  EXPECT_NEAR(motion_preferences::PregraspHeight(0.30), 0.38, 1e-12);
}

TEST(MotionPreferences, AvoidsJointLimitTargetsInsteadOfRelaxingBounds)
{
  EXPECT_FALSE(motion_preferences::ClearOfLimits(2.8973, -2.8973, 2.8973));
  EXPECT_FALSE(motion_preferences::ClearOfLimits(2.91, -2.8973, 2.8973));
  EXPECT_TRUE(motion_preferences::ClearOfLimits(2.80, -2.8973, 2.8973));
  EXPECT_FALSE(motion_preferences::ClearOfLimits(NAN, -2.8973, 2.8973));
}

TEST(MotionPreferences, AxialChoiceKeepsSuctionDownwardAndMatchesCurrentYaw)
{
  const std::array<double, 4> target{1., 0., 0., 0.};
  const std::array<double, 4> current{std::sqrt(.5), std::sqrt(.5), 0., 0.};
  const auto q = motion_preferences::AlignAxialYaw(target, current);
  EXPECT_NEAR(q[0], current[0], 1e-12);
  EXPECT_NEAR(q[1], current[1], 1e-12);
  EXPECT_NEAR(1-2*(q[0]*q[0]+q[1]*q[1]), -1., 1e-12);
  const auto rotated = motion_preferences::AlignAxialYaw(target, current, 1.5707963267948966);
  EXPECT_NEAR(1-2*(rotated[0]*rotated[0]+rotated[1]*rotated[1]), -1., 1e-12);
}

TEST(MotionPreferences, ExcursionIncludesIntermediateOvershootAndUsesNamedJointOrder)
{
  std::vector<double> a(7, 0.), b = a, c = a;
  b[2] = 2.; c[2] = .1;
  EXPECT_DOUBLE_EQ(motion_preferences::MaxExcursion({a,b,c}), 2.);
  EXPECT_TRUE(std::isinf(motion_preferences::MaxExcursion({{0.}})));
  auto values = motion_preferences::OrderedJoints(
    {"panda_joint7", "panda_joint6", "panda_joint5", "panda_joint4", "panda_joint3", "panda_joint2", "panda_joint1"},
    {7.,6.,5.,4.,3.,2.,1.});
  EXPECT_EQ(values, (std::vector<double>{1.,2.,3.,4.,5.,6.,7.}));
  EXPECT_TRUE(motion_preferences::OrderedJoints({"panda_joint1"}, {0.}).empty());
}

TEST(MotionPreferences, PenalizesWristTurnsWithoutWrappingBoundedJoints)
{
  std::vector<double> zero(7, 0.0), wrist = zero, nearby = zero;
  wrist[6] = 5.8;
  nearby[6] = 0.2;
  EXPECT_GT(motion_preferences::Distance(zero, wrist), 8.0);
  EXPECT_LT(motion_preferences::Distance(zero, nearby), 0.3);
}

TEST(MotionPreferences, PrefersShortPathsAndRejectsMalformedPoints)
{
  std::vector<double> zero(7, 0.0), middle = zero, end = zero;
  middle[0] = -1.0;
  end[0] = 1.0;
  EXPECT_GT(motion_preferences::PathLength({zero, middle, end}),
    motion_preferences::PathLength({zero, end}));
  EXPECT_TRUE(std::isinf(motion_preferences::PathLength({})));
  EXPECT_TRUE(std::isinf(motion_preferences::Distance({0.0}, {0.0})));
}

TEST(JointStateGuard, RequiresStableFreshWindow)
{
  motion_guard::StabilityWindow guard;
  EXPECT_FALSE(guard.Observe({0.0, 1.0}, 1.0, 0.0));
  EXPECT_FALSE(guard.Observe({0.0001, 1.0}, 1.1, 0.1));
  EXPECT_TRUE(guard.Observe({0.0002, 1.0}, 1.3, 0.3));
}

TEST(JointStateGuard, RepeatedTimestampCannotMasqueradeAsStableFeedback)
{
  motion_guard::StabilityWindow guard;
  EXPECT_FALSE(guard.Observe({0.0}, 1.0, 0.0));
  EXPECT_FALSE(guard.Observe({0.0}, 1.0, 0.3));
  EXPECT_FALSE(guard.Observe({0.0}, 1.0, 2.0));
}

TEST(JointStateGuard, AccumulatedDriftRestartsWindow)
{
  motion_guard::StabilityWindow guard;
  EXPECT_FALSE(guard.Observe({0.0}, 1.0, 0.0));
  EXPECT_FALSE(guard.Observe({0.0007}, 1.1, 0.1));
  EXPECT_FALSE(guard.Observe({0.0014}, 1.3, 0.3));
  EXPECT_FALSE(guard.Observe({0.0015}, 1.4, 0.4));
  EXPECT_TRUE(guard.Observe({0.0016}, 1.6, 0.6));
}

TEST(JointStateGuard, RejectsEmptyMalformedAndNonfiniteSamples)
{
  using motion_guard::MaxError;
  EXPECT_TRUE(std::isinf(MaxError({}, {})));
  EXPECT_TRUE(std::isinf(MaxError({0.0}, {0.0, 1.0})));
  EXPECT_TRUE(std::isinf(MaxError({NAN}, {0.0})));
  EXPECT_DOUBLE_EQ(MaxError({0.0, 1.0}, {0.004, 1.002}), 0.004);
}
