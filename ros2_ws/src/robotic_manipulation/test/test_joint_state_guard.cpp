#include <gtest/gtest.h>
#include "robotic_manipulation/joint_state_guard.h"
#include "robotic_manipulation/motion_preferences.h"

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
