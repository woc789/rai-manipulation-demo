#include <gtest/gtest.h>
#include "robotic_manipulation/joint_state_guard.h"

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
